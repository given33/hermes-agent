"""Convergent filesystem adapter for durable account write approvals."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


class WriteApprovalApplyError(RuntimeError):
    pass


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _written_text_hash(content: str) -> str:
    # skill_manager_tool opens text files with newline=None, so Windows writes
    # LF input as CRLF while Unix keeps LF. Hash the bytes that will be installed.
    rendered = content.replace("\n", os.linesep) if os.linesep != "\n" else content
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _memory_result(
    payload: dict[str, Any], entries: list[str], limit: int
) -> list[str]:
    from tools.memory_tool import ENTRY_DELIMITER, _scan_memory_content

    working = list(entries)

    def apply_one(operation: dict[str, Any], *, batch: bool) -> None:
        action = str(operation.get("action") or "")
        content = str(operation.get("content") or "").strip()
        old_text = str(operation.get("old_text") or "").strip()
        if action in {"add", "replace"}:
            if not content:
                raise WriteApprovalApplyError("memory content is required")
            scan_error = _scan_memory_content(content)
            if scan_error:
                raise WriteApprovalApplyError(scan_error)
        if action == "add":
            if content not in working:
                working.append(content)
            return
        if action not in {"replace", "remove"}:
            raise WriteApprovalApplyError(f"unsupported memory action: {action}")
        if not old_text:
            raise WriteApprovalApplyError("memory old_text is required")
        matches = [(index, value) for index, value in enumerate(working) if old_text in value]
        if not matches:
            raise WriteApprovalApplyError(f"memory entry no longer matches: {old_text}")
        if len({value for _, value in matches}) > 1:
            raise WriteApprovalApplyError(f"memory entry match is ambiguous: {old_text}")
        index = matches[0][0]
        if action == "replace":
            working[index] = content
        else:
            working.pop(index)
        if not batch and len(ENTRY_DELIMITER.join(working)) > limit:
            raise WriteApprovalApplyError("memory write exceeds the configured limit")

    action = str(payload.get("action") or "")
    if action == "batch":
        operations = payload.get("operations")
        if not isinstance(operations, list) or not operations:
            raise WriteApprovalApplyError("memory batch is empty")
        for operation in operations:
            if not isinstance(operation, dict):
                raise WriteApprovalApplyError("memory batch operation is invalid")
            apply_one(operation, batch=True)
        from tools.memory_tool import ENTRY_DELIMITER

        if len(ENTRY_DELIMITER.join(working)) > limit:
            raise WriteApprovalApplyError("memory batch exceeds the configured limit")
    else:
        apply_one(payload, batch=False)
    return working


def _prepare_memory(payload: dict[str, Any]) -> dict[str, Any]:
    from tools.memory_tool import load_on_disk_store

    target = str(payload.get("target") or "memory")
    if target not in {"memory", "user"}:
        raise WriteApprovalApplyError("unsupported memory target")
    store = load_on_disk_store()
    before = list(store._entries_for(target))
    after = _memory_result(payload, before, store._char_limit(target))
    return {
        "version": 1,
        "subsystem": "memory",
        "target": target,
        "before": _digest(before),
        "after": _digest(after),
    }


def _apply_memory(payload: dict[str, Any], plan: dict[str, Any]) -> tuple[bool, str]:
    from tools.memory_tool import load_on_disk_store

    target = str(plan.get("target") or "")
    if target not in {"memory", "user"}:
        return False, "approval memory plan has an invalid target"
    store = load_on_disk_store()
    path = store._path_for(target)
    with store._file_lock(path):
        backup = store._reload_target(target)
        if backup:
            return False, "memory changed outside Hermes; approval was not applied"
        current = list(store._entries_for(target))
        current_digest = _digest(current)
        if current_digest == str(plan.get("after") or ""):
            return True, ""
        if current_digest != str(plan.get("before") or ""):
            return False, "memory changed after approval was claimed"
        try:
            expected = _memory_result(payload, current, store._char_limit(target))
        except WriteApprovalApplyError as exc:
            return False, str(exc)
        if _digest(expected) != str(plan.get("after") or ""):
            return False, "approval memory plan no longer matches its payload"
        if expected != current:
            store._set_entries(target, expected)
            store.save_to_disk(target)
        return True, ""


def _tree_map(directory: Path) -> dict[str, str] | None:
    if not directory.exists():
        return None
    if not directory.is_dir() or directory.is_symlink():
        raise WriteApprovalApplyError("skill target is not a regular directory")
    result: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise WriteApprovalApplyError("skill contains a symbolic link")
        if path.is_file():
            relative = path.relative_to(directory).as_posix()
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _tree_digest(directory: Path) -> str:
    mapping = _tree_map(directory)
    return "absent" if mapping is None else _digest(mapping)


def _checked_skill_dir(path: Path) -> tuple[Path, str]:
    home = Path(get_hermes_home()).resolve()
    local_skills = (home / "skills").resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(local_skills)
        relative = resolved.relative_to(home).as_posix()
    except (OSError, ValueError) as exc:
        raise WriteApprovalApplyError(
            "mobile approvals may only modify the active profile's local skills"
        ) from exc
    return resolved, relative


def _prepare_skill(payload: dict[str, Any]) -> dict[str, Any]:
    import tools.skill_manager_tool as skills

    action = str(payload.get("action") or "")
    name = str(payload.get("name") or "")
    name_error = skills._validate_name(name)
    if name_error:
        raise WriteApprovalApplyError(name_error)

    if action == "create":
        category = payload.get("category")
        category_error = skills._validate_category(category)
        if category_error:
            raise WriteApprovalApplyError(category_error)
        content = payload.get("content")
        if not isinstance(content, str) or not content:
            raise WriteApprovalApplyError("skill content is required")
        for error in (
            skills._validate_frontmatter(content),
            skills._validate_content_size(content),
        ):
            if error:
                raise WriteApprovalApplyError(error)
        if skills._find_skill(name):
            raise WriteApprovalApplyError("skill already exists")
        skill_dir, relative = _checked_skill_dir(
            skills._resolve_skill_dir(name, category)
        )
        before_map = None
        after_map: dict[str, str] | None = {
            "SKILL.md": _written_text_hash(content)
        }
    else:
        existing = skills._find_skill(name)
        if not existing:
            raise WriteApprovalApplyError("skill was not found")
        skill_dir, relative = _checked_skill_dir(Path(existing["path"]))
        before_map = _tree_map(skill_dir)
        if before_map is None:
            raise WriteApprovalApplyError("skill was not found")
        after_map = dict(before_map)
        if action == "edit":
            content = payload.get("content")
            if not isinstance(content, str) or not content:
                raise WriteApprovalApplyError("skill content is required")
            for error in (
                skills._validate_frontmatter(content),
                skills._validate_content_size(content),
            ):
                if error:
                    raise WriteApprovalApplyError(error)
            after_map["SKILL.md"] = _written_text_hash(content)
        elif action == "patch":
            supplied_path = str(payload.get("file_path") or "")
            file_path = supplied_path or "SKILL.md"
            if supplied_path:
                error = skills._validate_file_path(file_path)
                if error:
                    raise WriteApprovalApplyError(error)
                target, error = skills._resolve_skill_target(skill_dir, file_path)
                if error or target is None:
                    raise WriteApprovalApplyError(error or "skill patch target was not found")
            else:
                target = skill_dir / "SKILL.md"
            if not target.is_file():
                raise WriteApprovalApplyError("skill patch target was not found")
            from tools.fuzzy_match import fuzzy_find_and_replace

            content = target.read_text(encoding="utf-8")
            updated, _count, _strategy, match_error = fuzzy_find_and_replace(
                content,
                str(payload.get("old_string") or ""),
                str(payload.get("new_string") or ""),
                bool(payload.get("replace_all")),
            )
            if match_error:
                raise WriteApprovalApplyError(match_error)
            relative_target = target.relative_to(skill_dir).as_posix()
            after_map[relative_target] = _written_text_hash(updated)
        elif action == "write_file":
            file_path = str(payload.get("file_path") or "")
            error = skills._validate_file_path(file_path)
            if error:
                raise WriteApprovalApplyError(error)
            content = payload.get("file_content")
            if not isinstance(content, str):
                raise WriteApprovalApplyError("skill file content is required")
            after_map[file_path] = _written_text_hash(content)
        elif action == "remove_file":
            file_path = str(payload.get("file_path") or "")
            error = skills._validate_file_path(file_path)
            if error:
                raise WriteApprovalApplyError(error)
            if file_path not in after_map:
                raise WriteApprovalApplyError("skill file was not found")
            after_map.pop(file_path)
        elif action == "delete":
            after_map = None
        else:
            raise WriteApprovalApplyError(f"unsupported skill action: {action}")

    return {
        "version": 1,
        "subsystem": "skills",
        "skill_dir": relative,
        "before": "absent" if before_map is None else _digest(before_map),
        "after": "absent" if after_map is None else _digest(after_map),
    }


def _apply_skill(payload: dict[str, Any], plan: dict[str, Any]) -> tuple[bool, str]:
    from tools.memory_tool import MemoryStore
    from tools.skill_manager_tool import apply_skill_pending

    home = Path(get_hermes_home()).resolve()
    relative = Path(str(plan.get("skill_dir") or ""))
    if relative.is_absolute() or ".." in relative.parts:
        return False, "approval skill plan has an invalid path"
    try:
        skill_dir, _ = _checked_skill_dir(home / relative)
    except WriteApprovalApplyError as exc:
        return False, str(exc)
    lock_target = home / ".write-approval-locks" / hashlib.sha256(
        str(skill_dir).encode("utf-8")
    ).hexdigest()
    with MemoryStore._file_lock(lock_target):
        try:
            current = _tree_digest(skill_dir)
        except (OSError, WriteApprovalApplyError) as exc:
            return False, str(exc)
        if current == str(plan.get("after") or ""):
            return True, ""
        if current != str(plan.get("before") or ""):
            return False, "skill changed after approval was claimed"
        try:
            result = json.loads(apply_skill_pending(payload))
        except Exception as exc:
            return False, str(exc)
        if not isinstance(result, dict) or not result.get("success"):
            return False, str((result or {}).get("error") or "skill write failed")
        try:
            if _tree_digest(skill_dir) != str(plan.get("after") or ""):
                return False, "skill write did not reach its recorded postcondition"
        except (OSError, WriteApprovalApplyError) as exc:
            return False, str(exc)
        return True, ""


def prepare_write_approval(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise WriteApprovalApplyError("approval payload is invalid")
    subsystem = str(record.get("subsystem") or "")
    if subsystem == "memory":
        return _prepare_memory(payload)
    if subsystem == "skills":
        return _prepare_skill(payload)
    raise WriteApprovalApplyError("approval subsystem is invalid")


def apply_write_approval(
    record: dict[str, Any], plan: dict[str, Any]
) -> tuple[bool, str]:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return False, "approval payload is invalid"
    subsystem = str(record.get("subsystem") or "")
    if str(plan.get("subsystem") or "") != subsystem:
        return False, "approval plan subsystem changed"
    if subsystem == "memory":
        return _apply_memory(payload, plan)
    if subsystem == "skills":
        return _apply_skill(payload, plan)
    return False, "approval subsystem is invalid"
