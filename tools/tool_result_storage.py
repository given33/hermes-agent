"""Tool result persistence -- preserves large outputs instead of truncating.

Defense against context-window overflow operates at three levels:

1. **Per-tool output cap** (inside each tool): Tools like search_files
   pre-truncate their own output before returning. This is the first line
   of defense and the only one the tool author controls.

2. **Per-result persistence** (maybe_persist_tool_result): After a tool
   returns, if its output exceeds the tool's registered threshold, an
   account-scoped hosted run stores one encrypted artifact and returns its
   durable identifier. Unscoped interactive runs may instead write into the
   active sandbox temp directory through env.execute(), replacing the
   in-context content with a bounded preview and a run-local file reference.

3. **Per-turn aggregate budget** (enforce_turn_budget): After all tool
   results in a single assistant turn are collected, if the total exceeds
   MAX_TURN_BUDGET_CHARS (200K), the largest non-persisted results are
   spilled to disk until the aggregate is under budget. This catches cases
   where many medium-sized results combine to overflow context.
"""

import hashlib
import json
import logging
import os
import re
import shlex
import uuid
from typing import Any

from hermes_runtime.session_context import get_session_env
from hermes_services import internal_hooks, tool_contract, tool_output_artifacts
from tools.budget_config import (
    DEFAULT_PREVIEW_SIZE_CHARS,
    BudgetConfig,
    DEFAULT_BUDGET,
)

logger = logging.getLogger(__name__)
PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"
STORAGE_DIR = "/tmp/hermes-results"
HEREDOC_MARKER = "HERMES_PERSIST_EOF"
_BUDGET_TOOL_NAME = "__budget_enforcement__"
_UNSAFE_RESULT_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_RESULT_FILENAME_STEM = 120


def _canonical_tool_result_text(content: Any) -> str:
    """Normalize structured tool output before string-only runtime hooks."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    try:
        return json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError, OverflowError):
        return str(content)


def _resolve_storage_dir(env) -> str:
    """Return the best temp-backed storage dir for this environment."""
    if env is not None:
        get_temp_dir = getattr(env, "get_temp_dir", None)
        if callable(get_temp_dir):
            try:
                temp_dir = get_temp_dir()
            except Exception as exc:
                logger.debug("Could not resolve env temp dir: %s", exc)
            else:
                if temp_dir:
                    temp_dir = temp_dir.rstrip("/") or "/"
                    return f"{temp_dir}/hermes-results"
    return STORAGE_DIR


def _safe_result_filename(tool_use_id: str) -> str:
    """Return a single safe filename for a tool result id."""
    raw_id = str(tool_use_id or "tool_result")
    safe_stem = _UNSAFE_RESULT_FILENAME_CHARS.sub("_", raw_id).strip("._-")
    changed = safe_stem != raw_id

    if not safe_stem:
        safe_stem = "tool_result"
        changed = True

    if changed or len(safe_stem) > _MAX_RESULT_FILENAME_STEM:
        digest = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:12]
        safe_stem = safe_stem[:_MAX_RESULT_FILENAME_STEM].rstrip("._-") or "tool_result"
        safe_stem = f"{safe_stem}_{digest}"

    return f"{safe_stem}.txt"


def generate_preview(content: str, max_chars: int = DEFAULT_PREVIEW_SIZE_CHARS) -> tuple[str, bool]:
    """Truncate at last newline within max_chars. Returns (preview, has_more)."""
    if len(content) <= max_chars:
        return content, False
    truncated = content[:max_chars]
    last_nl = truncated.rfind("\n")
    if last_nl > max_chars // 2:
        truncated = truncated[:last_nl + 1]
    return truncated, True


def _heredoc_marker(content: str) -> str:
    """Return a heredoc delimiter that doesn't collide with content."""
    if HEREDOC_MARKER not in content:
        return HEREDOC_MARKER
    return f"HERMES_PERSIST_{uuid.uuid4().hex[:8]}"


def _write_to_sandbox(content: str, remote_path: str, env) -> bool:
    """Write content into the sandbox via env.execute(). Returns True on success.

    Pushes ``content`` through stdin rather than embedding it in the command
    string. Linux's ``MAX_ARG_STRLEN`` caps any single argv element at 128 KB
    (32 * PAGE_SIZE), so the previous heredoc-in-the-command-string approach
    silently failed with ``OSError: [Errno 7] Argument list too long`` for any
    tool result over ~128 KB — exactly the case persistence exists to handle.
    Routing through stdin removes that ceiling on local + ssh (``_stdin_mode
    == "pipe"``); remote backends with ``_stdin_mode == "heredoc"`` keep their
    existing API-body sized limit, which is orders of magnitude larger than
    the exec-arg ceiling.
    """
    storage_dir = os.path.dirname(remote_path)
    cmd = f"mkdir -p {shlex.quote(storage_dir)} && cat > {shlex.quote(remote_path)}"
    result = env.execute(cmd, timeout=30, stdin_data=content)
    return result.get("returncode", 1) == 0


def _build_persisted_message(
    preview: str,
    has_more: bool,
    original_size: int,
    file_path: str,
    artifact_id: str = "",
    retention_error: str = "",
) -> str:
    """Build the <persisted-output> replacement block."""
    size_kb = original_size / 1024
    if size_kb >= 1024:
        size_str = f"{size_kb / 1024:.1f} MB"
    else:
        size_str = f"{size_kb:.1f} KB"

    msg = f"{PERSISTED_OUTPUT_TAG}\n"
    msg += f"This tool result was too large ({original_size:,} characters, {size_str}).\n"
    if artifact_id:
        msg += f"Account artifact: {artifact_id}\n"
    if file_path:
        msg += f"Full output saved to: {file_path}\n"
        msg += "Use read_file with offset and limit during this run to access specific sections.\n"
    if retention_error:
        msg += f"Retention status: {retention_error}\n"
    msg += "\n"
    msg += f"Preview (first {len(preview)} chars):\n"
    msg += preview
    if has_more:
        msg += "\n..."
    msg += f"\n{PERSISTED_OUTPUT_CLOSING_TAG}"
    return msg


def maybe_persist_tool_result(
    content: Any,
    tool_name: str,
    tool_use_id: str,
    env=None,
    config: BudgetConfig = DEFAULT_BUDGET,
    threshold: int | float | None = None,
    *,
    apply_hooks: bool = True,
) -> str:
    """Layer 2: retain oversized output and return a bounded reference.

    Account-scoped hosted runs use encrypted artifacts and fail closed to a
    preview when retention fails. Other runs may write through ``env`` so the
    active execution backend can read the full result during the same run.

    Args:
        content: Raw tool result. Structured values become canonical JSON.
        tool_name: Name of the tool (used for threshold lookup).
        tool_use_id: Unique ID for this tool call (used as filename).
        env: The active BaseEnvironment instance, or None.
        config: BudgetConfig controlling thresholds and preview size.
        threshold: Explicit override; takes precedence over config resolution.
        apply_hooks: Apply the trusted after-tool-result hook exactly once.

    Returns:
        Original content if small, or <persisted-output> replacement.
    """
    content = _canonical_tool_result_text(content)
    if apply_hooks:
        hook_result = internal_hooks.run_internal_hooks(
            "after_tool_result",
            content,
            tool_name=tool_name,
            tool_use_id=tool_use_id,
        )
        content = str(hook_result.payload)
        hook_trace = hook_result.trace
        if hook_trace:
            logger.debug("after_tool_result hooks for %s: %s", tool_use_id, hook_trace)
    effective_threshold = threshold if threshold is not None else config.resolve_threshold(tool_name)
    output_policy = tool_contract.resolve_tool_contract(tool_name).output_policy

    if output_policy == "inline" or effective_threshold == float("inf"):
        return content

    if len(content) <= effective_threshold:
        return content

    storage_dir = _resolve_storage_dir(env)
    remote_path = f"{storage_dir}/{_safe_result_filename(tool_use_id)}"
    preview, has_more = generate_preview(content, max_chars=config.preview_size)
    artifact_id = ""
    owner_id = str(get_session_env("HERMES_TOOL_ARTIFACT_OWNER") or "").strip()
    account_generation = str(
        get_session_env("HERMES_ACCOUNT_GENERATION") or "legacy"
    ).strip() or "legacy"
    artifact_root = str(get_session_env("HERMES_TOOL_ARTIFACT_ROOT") or "").strip()
    artifact_error = ""
    if owner_id and artifact_root:
        try:
            from pathlib import Path

            artifact = tool_output_artifacts.EncryptedToolArtifactStore(
                Path(artifact_root)
            ).put(
                owner_id=owner_id,
                account_generation=account_generation,
                conversation_id=str(
                    get_session_env("HERMES_TOOL_ARTIFACT_CONVERSATION") or ""
                ),
                turn_id=str(get_session_env("HERMES_TOOL_ARTIFACT_TURN") or ""),
                tool_call_id=tool_use_id,
                tool_name=tool_name,
                content=content,
            )
            artifact_id = str(artifact.get("id") or "")
        except Exception as exc:
            logger.warning("Encrypted tool artifact write failed for %s: %s", tool_use_id, exc)
            artifact_error = "encrypted artifact storage failed; full output was not retained"
    elif owner_id:
        artifact_error = "encrypted artifact storage is unavailable; full output was not retained"

    # Account-scoped hosted runs must have exactly one retained full-output
    # copy. Writing the same plaintext into a sandbox would escape account
    # generation deletion and defeat the encrypted artifact boundary.
    if artifact_id:
        return _build_persisted_message(
            preview,
            has_more,
            len(content),
            "",
            artifact_id,
        )

    # Once a hosted account boundary is present, confidentiality is
    # fail-closed. Never downgrade to a process/sandbox plaintext copy when
    # encrypted persistence is unavailable.
    if owner_id:
        return _build_persisted_message(
            preview,
            has_more,
            len(content),
            "",
            retention_error=artifact_error,
        )

    if env is not None:
        try:
            if _write_to_sandbox(content, remote_path, env):
                logger.info(
                    "Persisted large tool result: %s (%s, %d chars -> %s)",
                    tool_name, tool_use_id, len(content), remote_path,
                )
                return _build_persisted_message(
                    preview,
                    has_more,
                    len(content),
                    remote_path,
                    artifact_id,
                )
        except Exception as exc:
            logger.warning("Sandbox write failed for %s: %s", tool_use_id, exc)

    logger.info(
        "Inline-truncating large tool result: %s (%d chars, no sandbox write)",
        tool_name, len(content),
    )
    return (
        f"{preview}\n\n"
        f"[Truncated: tool response was {len(content):,} chars. "
        f"Full output could not be saved to sandbox.]"
    )


def enforce_turn_budget(
    tool_messages: list[dict],
    env=None,
    config: BudgetConfig = DEFAULT_BUDGET,
) -> list[dict]:
    """Layer 3: enforce aggregate budget across all tool results in a turn.

    If total chars exceed budget, persist the largest non-persisted results
    first (via sandbox write) until under budget. Already-persisted results
    are skipped.

    Mutates the list in-place and returns it.
    """
    candidates = []
    total_size = 0
    for i, msg in enumerate(tool_messages):
        content = msg.get("content", "")
        size = len(content)
        total_size += size
        if PERSISTED_OUTPUT_TAG not in content:
            candidates.append((i, size))

    if total_size <= config.turn_budget:
        return tool_messages

    candidates.sort(key=lambda x: x[1], reverse=True)

    for idx, size in candidates:
        if total_size <= config.turn_budget:
            break
        msg = tool_messages[idx]
        content = msg["content"]
        tool_use_id = msg.get("tool_call_id", f"budget_{idx}")

        replacement = maybe_persist_tool_result(
            content=content,
            tool_name=_BUDGET_TOOL_NAME,
            tool_use_id=tool_use_id,
            env=env,
            config=config,
            threshold=0,
            apply_hooks=False,
        )
        if replacement != content:
            total_size -= size
            total_size += len(replacement)
            tool_messages[idx]["content"] = replacement
            logger.info(
                "Budget enforcement: persisted tool result %s (%d chars)",
                tool_use_id, size,
            )

    return tool_messages
