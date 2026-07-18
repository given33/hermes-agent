"""Official dashboard collaboration plugin.

Group chat turns execute the existing Hermes CLI against named profiles, so
responses use the same model, skills, MCP servers, memory, and session store as
ordinary official WebUI chats. Workflow rendering delegates to the bundled
Kanban dashboard API rather than introducing a second scheduler.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
import hashlib
import hmac
import inspect
import importlib.util
import json
import logging
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, unquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

try:
    from hermes_cli.cloud_file_library import (
        CloudFileLibrary,
        LOCAL_OWNER_ID,
        owner_id_from_request,
        parse_date_filter,
    )
except ModuleNotFoundError as exc:
    if exc.name != "hermes_cli.cloud_file_library":
        raise
    runtime_module = (
        Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        / "runtime"
        / "cloud_file_library.py"
    )
    runtime_stat = runtime_module.stat()
    if runtime_stat.st_uid != os.getuid() or runtime_stat.st_mode & 0o022:
        raise RuntimeError("Cloud file runtime module has unsafe ownership or mode")
    runtime_spec = importlib.util.spec_from_file_location(
        "hermes_collaboration_cloud_file_library",
        runtime_module,
    )
    if runtime_spec is None or runtime_spec.loader is None:
        raise RuntimeError("Cloud file runtime module could not be loaded")
    runtime_library = importlib.util.module_from_spec(runtime_spec)
    sys.modules[runtime_spec.name] = runtime_library
    runtime_spec.loader.exec_module(runtime_library)
    CloudFileLibrary = runtime_library.CloudFileLibrary
    LOCAL_OWNER_ID = runtime_library.LOCAL_OWNER_ID
    owner_id_from_request = runtime_library.owner_id_from_request
    parse_date_filter = runtime_library.parse_date_filter
from hermes_cli.config import get_hermes_home
from hermes_cli.profiles import list_profiles


@asynccontextmanager
async def collaboration_dashboard_lifespan(_app):
    """Resume persisted work as soon as the dashboard process starts."""
    try:
        state = load_single_state()
        resume_unfinished_hosted_workflows(state.get("conversations") or [])
    except Exception:
        logging.getLogger(__name__).exception(
            "Failed to resume collaboration hosted workflows during startup"
        )
    yield


router = APIRouter(lifespan=collaboration_dashboard_lifespan)
_STATE_LOCK = threading.RLock()
_HOSTED_THREADS_LOCK = threading.Lock()
_HOSTED_THREADS: dict[str, threading.Thread] = {}
_HOSTED_CONVERSATION_LOCKS_LOCK = threading.Lock()
_HOSTED_CONVERSATION_LOCKS: dict[str, threading.Lock] = {}
_MOBILE_NOTIFICATION_DISPATCH_CONDITION = threading.Condition()
_MOBILE_NOTIFICATION_DISPATCH_THREAD: Optional[threading.Thread] = None
_MOBILE_NOTIFICATION_PENDING: dict[str, tuple[str, str, int]] = {}
_HOSTED_UPDATE_CONDITION = threading.Condition()
_HOSTED_UPDATE_REVISION = 0
_HOSTED_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
_MOBILE_NOTIFICATION_TERMINAL_STATUSES = {
    "delivered",
    "no_recipients",
    "permanent_failure",
}
_HOSTED_TRANSIENT_RETRIES = 1
_HOSTED_REWORK_LIMIT = 2
_HOSTED_EVENT_FLUSH_SECONDS = 0.45
_REMOTE_CONTRACT_VERSION = 1
_REMOTE_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
_REMOTE_RUN_LEASE_SECONDS = 60
_MAX_REMOTE_RUNS_PER_PULL = 20
_PROMPT_HISTORY = 24
_MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024
_MAX_CONVERSATION_TITLE_CHARS = 18
_ATTACHMENT_BUCKETS = {"uploads", "outputs"}
_ACCOUNT_FILE_MIGRATION_LOCK = threading.Lock()
_ACCOUNT_FILE_MIGRATION_VERSION = "conversation-files-v1"
_WORK_MARKERS = (
    "完成",
    "执行",
    "修改",
    "合并",
    "创建",
    "开发",
    "部署",
    "安装",
    "配置",
    "修复",
    "优化",
    "测试",
    "检查",
    "调试",
    "运行",
    "整理",
    "生成",
    "写一个",
    "做一个",
    "workflow",
    "deploy",
    "build",
    "fix",
    "implement",
)
_PC_MARKERS = ("本地电脑", "windows", "wsl", "pc", "桌面", "处理器", "gpu")
_DBB3_MARKERS = ("dbb3", "linux", "armbian", "网关", "gateway")
_COMPLEX_WORK_MARKERS = (
    "修改代码",
    "修复",
    "部署",
    "安装",
    "配置",
    "运行测试",
    "工作流",
    "拆分任务",
    "协作",
    "上传文件",
    "生成ppt",
    "生成 ppt",
    "写ppt",
    "写 ppt",
    "交付",
    "全部完成",
    "多步骤",
    "数据库",
    "服务器",
    "本地电脑",
    "dbb3",
)
_SIMPLE_CHAT_MARKERS = (
    "你好",
    "谢谢",
    "在吗",
    "怎么样",
    "是什么",
    "为什么",
    "解释",
    "介绍",
    "总结一下",
    "聊聊",
)
_MULTI_STEP_MARKERS = ("然后", "接着", "并且", "同时", "最后", "之后", "以及")
_DIRECT_ARTIFACT_MARKERS = (
    "ppt",
    "pptx",
    "演示文稿",
    "幻灯片",
    "powerpoint",
    "word",
    "docx",
    "pdf",
    "excel",
    "xlsx",
    "csv",
    "压缩包",
    "zip文件",
    "zip 文件",
)
_ARTIFACT_ACTION_MARKERS = (
    "生成",
    "制作",
    "做一个",
    "做个",
    "写一个",
    "写个",
    "导出",
    "保存为",
    "保存成",
    "打包",
    "下载",
    "发给我",
    "上传给我",
    "交付",
    "create",
    "generate",
    "produce",
    "export",
    "save as",
    "send me",
    "upload",
    "deliver",
    "attach",
    "package",
)
_ARTIFACT_NOUN_MARKERS = (
    "文档",
    "报告",
    "表格",
    "图片",
    "文件",
    "附件",
    "file",
    "document",
    "report",
    "spreadsheet",
    "image",
    "attachment",
    "deliverable",
    "archive",
)


def _configured_connector_token() -> str:
    """Read the connector credential without persisting or logging it."""

    value = os.environ.get("HERMES_COLLABORATION_CONNECTOR_TOKEN", "").strip()
    if value:
        return value
    path = os.environ.get("HERMES_COLLABORATION_CONNECTOR_TOKEN_FILE", "").strip()
    candidates = [path] if path else []
    candidates.append("/etc/hermes-mobile/connector_token")
    candidates.append("/etc/hermes-agent/collaboration-connector-token")
    for candidate in candidates:
        if not candidate:
            continue
        try:
            value = Path(candidate).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if value:
            return value
    return ""


def _configured_connector_tokens() -> dict[str, str]:
    """Return connector credentials keyed by their immutable device id.

    The original single-token deployment remains supported, but that token is
    deliberately bound only to DBB3. Additional devices use the JSON secret
    map so presenting a valid token never lets a caller choose its identity.
    """

    configured: dict[str, str] = {}
    legacy = _configured_connector_token()
    if legacy:
        configured["dbb3-primary"] = legacy
    raw = os.environ.get("HERMES_COLLABORATION_CONNECTOR_TOKENS", "").strip()
    map_file = os.environ.get(
        "HERMES_COLLABORATION_CONNECTOR_TOKENS_FILE",
        "",
    ).strip()
    if map_file:
        try:
            raw = Path(map_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raw = ""
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = {}
        if isinstance(parsed, dict):
            for connector_id, token in parsed.items():
                normalized_id = str(connector_id or "").strip()[:128]
                normalized_token = str(token or "").strip()
                if normalized_id and normalized_token:
                    configured[normalized_id] = normalized_token
    return configured


def _connector_bearer(request: Request) -> str:
    authorization = str(request.headers.get("authorization") or "")
    scheme, separator, token = authorization.partition(" ")
    return token.strip() if separator and scheme.lower() == "bearer" else ""


def _connector_identity(request: Request) -> str:
    supplied = _connector_bearer(request)
    claimed = str(request.headers.get("x-connector-id") or "").strip()[:128]
    if not supplied or not claimed:
        return ""
    expected = _configured_connector_tokens().get(claimed, "")
    return claimed if expected and hmac.compare_digest(supplied, expected) else ""


def _connector_authorized(request: Request) -> bool:
    return bool(_connector_identity(request))


def _require_connector(request: Request) -> str:
    if not _configured_connector_tokens():
        raise HTTPException(status_code=503, detail="Connector credential is not configured")
    connector_id = _connector_identity(request)
    if not connector_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return connector_id


def _register_connector_token_auth() -> None:
    """Register the connector prefix with the dashboard bearer seam.

    The plugin still checks the credential in each handler. Registration lets
    service callers bypass the interactive cookie gate on a public dashboard.
    """

    try:
        from hermes_cli.dashboard_auth import (
            DashboardAuthProvider,
            LoginStart,
            Session,
            TokenPrincipal,
            get_provider,
            register_provider,
        )
        from hermes_cli.dashboard_auth.token_auth import register_optional_token_prefix
    except Exception:
        return

    class _ConnectorTokenProvider(DashboardAuthProvider):
        name = "collaboration-connector"
        display_name = "Collaboration Connector"
        supports_session = False
        supports_token = True

        def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
            matches = [
                connector_id
                for connector_id, expected in _configured_connector_tokens().items()
                if token and hmac.compare_digest(token, expected)
            ]
            if len(matches) != 1:
                return None
            return TokenPrincipal(
                principal=matches[0],
                provider=self.name,
                scopes=("collaboration:connector",),
            )

        def start_login(self, *, redirect_uri: str) -> LoginStart:
            raise NotImplementedError("connector credentials are non-interactive")

        def complete_login(self, *, code: str, state: str, code_verifier: str, redirect_uri: str) -> Session:
            raise NotImplementedError("connector credentials are non-interactive")

        def verify_session(self, *, access_token: str) -> Optional[Session]:
            return None

        def refresh_session(self, *, refresh_token: str) -> Session:
            raise NotImplementedError("connector credentials are non-interactive")

        def revoke_session(self, *, refresh_token: str) -> None:
            return None

    try:
        if get_provider("collaboration-connector") is None:
            register_provider(_ConnectorTokenProvider())
        register_optional_token_prefix(
            "/api/plugins/collaboration/connector",
            required_scope="collaboration:connector",
        )
    except Exception:
        logging.getLogger(__name__).debug(
            "Collaboration connector token seam registration skipped",
            exc_info=True,
        )


_register_connector_token_auth()


def state_path() -> Path:
    return Path(get_hermes_home()) / "collaboration" / "rooms.json"


def single_state_path() -> Path:
    return Path(get_hermes_home()) / "collaboration" / "single.json"


def safe_attachment_name(filename: str) -> str:
    name = Path(str(filename or "").replace("\x00", "")).name.strip()
    if name in {"", ".", ".."}:
        raise ValueError("附件文件名无效")
    return name


def conversation_files_root(conversation_id: str) -> Path:
    return (
        Path(get_hermes_home())
        / "collaboration"
        / "files"
        / conversation_id
    )


def _conversation_file_dir(conversation_id: str, bucket: str) -> Path:
    if bucket not in _ATTACHMENT_BUCKETS:
        raise HTTPException(status_code=404, detail="附件目录不存在")
    target = conversation_files_root(conversation_id) / bucket
    target.mkdir(parents=True, exist_ok=True)
    return target


def _hosted_turn_output_dir(conversation_id: str, turn_id: str) -> Path:
    normalized_turn_id = str(turn_id or "").strip()
    if not normalized_turn_id:
        raise ValueError("turn_id is required")
    turn_key = hashlib.sha256(normalized_turn_id.encode("utf-8")).hexdigest()[:32]
    target = _conversation_file_dir(conversation_id, "outputs") / "turns" / turn_key
    target.mkdir(parents=True, exist_ok=True)
    return target


def _hosted_output_paths(
    conversation_id: str,
    run: dict[str, Any],
) -> tuple[Path, Path]:
    output_root = _conversation_file_dir(conversation_id, "outputs").resolve()
    configured = str(run.get("output_dir") or "").strip()
    output_dir = Path(configured).resolve() if configured else output_root
    if not output_dir.is_relative_to(output_root):
        raise RuntimeError("Hosted output directory escapes the conversation output root")
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_root, output_dir


def _output_file_signatures(directory: Path) -> dict[str, str]:
    """Hash a stable output snapshot so adjacent turns cannot share artifacts."""

    if not directory.exists():
        return {}
    resolved_root = directory.resolve(strict=True)
    signatures: dict[str, str] = {}
    for candidate in sorted(directory.rglob("*")):
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or (candidate.name.startswith(".") and candidate.name.endswith(".upload"))
        ):
            continue
        before = candidate.stat()
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(resolved_root):
            continue
        digest = hashlib.sha256()
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = candidate.stat()
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise OSError(f"Output changed while its turn baseline was captured: {candidate}")
        signatures[resolved.relative_to(resolved_root).as_posix()] = digest.hexdigest()
    return signatures


def _ensure_hosted_output_baseline(
    conversation_id: str,
    run: dict[str, Any],
) -> None:
    if not bool(run.get("artifact_required")) or "output_baseline" in run:
        return
    _output_root, output_dir = _hosted_output_paths(conversation_id, run)
    run["output_baseline"] = _output_file_signatures(output_dir)
    run["output_baseline_captured_at"] = int(time.time() * 1000)


def _attachment_record(
    conversation_id: str,
    bucket: str,
    path: Path,
) -> dict[str, Any]:
    stat = path.stat()
    relative_path = path.relative_to(_conversation_file_dir(conversation_id, bucket))
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded_relative = "/".join(
        quote(part, safe="")
        for part in relative_path.parts
    )
    return {
        "id": f"{bucket}:{relative_path.as_posix()}",
        "name": path.name,
        "bucket": bucket,
        "relative_path": relative_path.as_posix(),
        "size": stat.st_size,
        "mime_type": mime_type,
        "updated_at": int(stat.st_mtime * 1000),
        "download_url": (
            "/api/plugins/collaboration/single/conversations/"
            f"{conversation_id}/attachments/{bucket}/{encoded_relative}"
        ),
    }


def _list_conversation_attachments(conversation_id: str) -> list[dict[str, Any]]:
    # Hosted workflows call this function while preparing their final report.
    # Once a conversation has an authenticated owner, publish outputs into the
    # durable account library before exposing them to that report. Legacy
    # unowned conversations retain the original directory-backed response.
    try:
        with _STATE_LOCK:
            state = load_single_state()
            conversation = next(
                (
                    item
                    for item in state.get("conversations") or []
                    if item.get("id") == conversation_id
                ),
                None,
            )
            owner_id = (
                str(conversation.get("owner_id") or "").strip()
                if isinstance(conversation, dict)
                else ""
            )
        if owner_id and isinstance(conversation, dict):
            try:
                _sync_conversation_files(owner_id, conversation)
            except Exception:
                logging.getLogger(__name__).exception(
                    "Failed to publish conversation files into account library"
                )
            return _conversation_library_attachments(owner_id, conversation_id)
    except Exception:
        logging.getLogger(__name__).exception(
            "Failed to resolve conversation file ownership"
        )

    attachments: list[dict[str, Any]] = []
    for bucket in sorted(_ATTACHMENT_BUCKETS):
        root = _conversation_file_dir(conversation_id, bucket)
        for path in sorted(root.rglob("*")):
            if path.is_file():
                attachments.append(
                    _attachment_record(conversation_id, bucket, path)
                )
    return attachments


def _hosted_turn_output_attachments(
    conversation_id: str,
    turn_id: str,
    started_at: int,
) -> list[dict[str, Any]]:
    """Publish and return outputs owned by exactly one hosted turn."""

    del started_at  # Timestamp proximity is not proof that a turn created a file.
    normalized_turn_id = str(turn_id or "").strip()
    if not normalized_turn_id:
        return []
    with _STATE_LOCK:
        state = load_single_state()
        conversation = _conversation_by_id(state, conversation_id)
        run = (conversation.get("hosted_turns") or {}).get(normalized_turn_id)
        if not isinstance(run, dict):
            return []
        run_snapshot = dict(run)
        owner_id = str(conversation.get("owner_id") or LOCAL_OWNER_ID).strip()
        profile = next(
            (
                str(item).strip()
                for item in run.get("profiles") or []
                if str(item).strip()
            ),
            str(conversation.get("profile") or "default").strip() or "default",
        )

    output_root, output_dir = _hosted_output_paths(conversation_id, run_snapshot)
    baseline = run_snapshot.get("output_baseline")
    library = _file_library()
    if isinstance(baseline, dict):
        normalized_baseline = {
            str(relative): str(digest)
            for relative, digest in baseline.items()
            if str(relative) and re.fullmatch(r"[0-9a-f]{64}", str(digest))
        }
        if len(normalized_baseline) == len(baseline):
            current = _output_file_signatures(output_dir)
            origin_root = f"conversation:{conversation_id}:outputs"
            output_relative = output_dir.relative_to(output_root).as_posix()
            origin_prefix = (
                f"{origin_root}:{output_relative}"
                if output_relative != "."
                else origin_root
            )
            if not normalized_baseline:
                library.sync_directory(
                    owner_id,
                    output_dir,
                    source="model_output",
                    conversation_id=conversation_id,
                    turn_id=normalized_turn_id,
                    profile=profile,
                    origin_prefix=origin_prefix,
                    strict=True,
                )
            else:
                for relative, digest in current.items():
                    if normalized_baseline.get(relative) == digest:
                        continue
                    candidate = (output_dir / relative).resolve(strict=True)
                    if not candidate.is_relative_to(output_dir):
                        continue
                    record = library.ingest_file(
                        owner_id,
                        candidate,
                        name=candidate.name,
                        source="model_output",
                        conversation_id=conversation_id,
                        turn_id=normalized_turn_id,
                        profile=profile,
                        origin_key=f"{origin_prefix}:{relative}",
                        allowed_roots=[output_dir],
                        restore_deleted=False,
                    )
                    if record is not None and str(record.get("sha256") or "") != digest:
                        raise OSError(f"Output changed while it was indexed: {candidate}")

    attachments: list[dict[str, Any]] = []
    offset = 0
    while True:
        page, total = library.list_files(
            owner_id,
            source="model_output",
            status="available",
            conversation_id=conversation_id,
            turn_id=normalized_turn_id,
            limit=200,
            offset=offset,
        )
        attachments.extend(_library_attachment(record) for record in page)
        offset += len(page)
        if not page or offset >= total:
            break
    return attachments


class StateStoreError(RuntimeError):
    """Raised when a collaboration state store needs operator recovery."""


def _state_backup_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.bak")


def _state_quarantine_paths(target: Path) -> list[Path]:
    return sorted(target.parent.glob(f"{target.name}.corrupt.*"))


def _state_path_present(target: Path) -> bool:
    try:
        target.lstat()
    except FileNotFoundError:
        return False
    return True


def _validate_state_document(
    data: Any,
    collection_key: str,
    source: Path,
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{source} must contain a JSON object")
    collection = data.get(collection_key)
    if not isinstance(collection, list):
        raise ValueError(f"{source} must contain a {collection_key!r} list")
    return data


def _read_state_document(target: Path, collection_key: str) -> dict[str, Any]:
    raw = target.read_text(encoding="utf-8")
    return _validate_state_document(json.loads(raw), collection_key, target)


def _fsync_parent_directory(target: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(target.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_state_document(target: Path, data: dict[str, Any]) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_parent_directory(target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _quarantine_state_file(target: Path) -> Path:
    quarantine = target.with_name(
        f"{target.name}.corrupt.{time.time_ns()}.{uuid.uuid4().hex[:8]}"
    )
    try:
        os.replace(target, quarantine)
        try:
            quarantine.chmod(0o600, follow_symlinks=False)
        except (NotImplementedError, OSError):
            pass
        _fsync_parent_directory(target)
    except OSError as exc:
        raise StateStoreError(
            f"Could not isolate unreadable collaboration state {target}: {exc}"
        ) from exc
    return quarantine


def _restore_state_backup(
    target: Path,
    collection_key: str,
    *,
    primary_error: BaseException | None = None,
    quarantine: Path | None = None,
) -> dict[str, Any]:
    backup = _state_backup_path(target)
    try:
        data = _read_state_document(backup, collection_key)
    except FileNotFoundError as backup_error:
        detail = f"; corrupt data preserved at {quarantine}" if quarantine else ""
        raise StateStoreError(
            f"Collaboration state {target} is unreadable and has no valid backup{detail}"
        ) from (primary_error or backup_error)
    except (OSError, UnicodeError, ValueError, TypeError) as backup_error:
        backup_quarantine = _quarantine_state_file(backup)
        detail = f"; corrupt data preserved at {quarantine}" if quarantine else ""
        raise StateStoreError(
            f"Collaboration state {target} and its backup are unreadable{detail}; "
            f"backup preserved at {backup_quarantine}"
        ) from backup_error
    try:
        _atomic_write_state_document(target, data)
    except OSError as restore_error:
        raise StateStoreError(
            f"Could not restore collaboration state {target} from {backup}"
        ) from restore_error
    logging.getLogger(__name__).error(
        "Recovered collaboration state %s from %s; damaged primary is at %s",
        target,
        backup,
        quarantine or "<missing>",
    )
    return data


def _load_state_store(target: Path, collection_key: str) -> dict[str, Any]:
    try:
        return _read_state_document(target, collection_key)
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, ValueError, TypeError) as primary_error:
        quarantine = _quarantine_state_file(target)
        return _restore_state_backup(
            target,
            collection_key,
            primary_error=primary_error,
            quarantine=quarantine,
        )

    if _state_path_present(_state_backup_path(target)):
        return _restore_state_backup(target, collection_key)
    quarantines = _state_quarantine_paths(target)
    if quarantines:
        raise StateStoreError(
            f"Collaboration state {target} is awaiting recovery; "
            f"preserved data: {quarantines[-1]}"
        )
    return {collection_key: []}


def _save_state_store(
    target: Path,
    state: dict[str, Any],
    collection_key: str,
) -> None:
    _validate_state_document(state, collection_key, target)
    previous: dict[str, Any] | None = None
    if (
        _state_path_present(target)
        or _state_path_present(_state_backup_path(target))
        or _state_quarantine_paths(target)
    ):
        previous = _load_state_store(target, collection_key)

    # Keep the last known-good document in a separately atomic file. On first
    # creation the backup is written first, so a crash cannot leave an empty
    # store with no recovery source.
    _atomic_write_state_document(
        _state_backup_path(target),
        previous if previous is not None else state,
    )
    _atomic_write_state_document(target, state)


def load_state(path: Optional[Path] = None) -> dict[str, Any]:
    target = path or state_path()
    data = _load_state_store(target, "rooms")
    return {"rooms": data["rooms"]}


def load_single_state(path: Optional[Path] = None) -> dict[str, Any]:
    target = path or single_state_path()
    data = _load_state_store(target, "conversations")
    normalized_conversations = data["conversations"]
    for conversation in normalized_conversations:
        if not isinstance(conversation, dict):
            continue
        messages = conversation.get("messages")
        if isinstance(messages, list):
            conversation["messages"] = normalize_stored_conversation_messages(
                messages
            )
        if not isinstance(conversation.get("hosted_turns"), dict):
            conversation["hosted_turns"] = {}
    return {"conversations": normalized_conversations}


def summarize_task_title(content: str) -> str:
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    if not text:
        return "新对话"
    text = text.split("本会话交付文件目录：", 1)[0].strip()
    reply_match = re.search(
        r"(?:^|[，,。；;])\s*只回复\s*[：:]?\s*([A-Za-z0-9][A-Za-z0-9_. -]{1,40})",
        text,
    )
    if reply_match:
        return reply_match.group(1).strip()[:_MAX_CONVERSATION_TITLE_CHARS]
    clauses = [
        clause.strip(" ：:，,。；;！？!?\"'`()[]{}")
        for clause in re.split(r"[\n，,。；;！？!?:：]+", text)
        if clause.strip()
    ] or [text]
    action_weights = {
        "修复": 10,
        "优化": 10,
        "部署": 10,
        "配置": 10,
        "创建": 10,
        "生成": 10,
        "安装": 9,
        "更新": 9,
        "删除": 9,
        "验收": 9,
        "验证": 8,
        "测试": 8,
        "继续": 8,
        "接着": 8,
        "检查": 7,
        "总结": 7,
        "整理": 7,
        "说明": 7,
        "解释": 7,
        "精炼": 7,
        "运行": 5,
        "调用": 4,
        "完成": 2,
    }
    topic_markers = (
        "Hermes",
        "hermes",
        "DBB3",
        "dbb3",
        "本地电脑",
        "iOS",
        "ios",
        "后台托管",
        "会话",
        "标题",
        "看板",
        "网络",
        "输入框",
        "定时任务",
        "自我进化",
        "terminal",
    )
    negative_markers = ("不要", "不调用", "无需", "禁止", "只回复")
    focused_clauses = [
        clause
        for clause in clauses
        if not any(marker in clause for marker in negative_markers)
    ]
    if focused_clauses:
        clauses = focused_clauses

    def score(clause: str) -> tuple[int, int]:
        value = sum(weight for marker, weight in action_weights.items() if marker in clause)
        value += sum(2 for marker in topic_markers if marker in clause)
        if any(marker in clause for marker in negative_markers):
            value -= 20
        return value, -len(clause)

    title = max(clauses, key=score)
    title = re.sub(
        r"^(?:请用[一二三四五六七八九十\d]+句话|请(?:你)?|麻烦(?:你)?|帮我|你(?:去|帮我)?|我(?:希望|想要|要)|能不能|可以)+",
        "",
        title,
    ).strip()
    title = re.sub(r"(?:已)?完成$", "", title).strip()
    if not title:
        title = clauses[0]
    if len(title) > _MAX_CONVERSATION_TITLE_CHARS:
        title = title[: _MAX_CONVERSATION_TITLE_CHARS - 1].rstrip() + "…"
    return title or "新对话"


def compact_conversation_title(conversation: dict[str, Any]) -> bool:
    current = str(conversation.get("title") or "").strip()
    poor_title = any(
        marker in current
        for marker in ("不要", "不调用", "无需", "禁止", "只回复")
    ) or current.startswith("回复 ")
    if len(current) <= _MAX_CONVERSATION_TITLE_CHARS and not poor_title:
        return False
    first_user_message = next(
        (
            str(message.get("content") or "").strip()
            for message in conversation.get("messages") or []
            if message.get("role") == "user" and message.get("content")
        ),
        current,
    )
    compacted = summarize_task_title(first_user_message)
    if compacted == current:
        return False
    conversation["title"] = compacted
    return True


def save_state(state: dict[str, Any], path: Optional[Path] = None) -> None:
    target = path or state_path()
    _save_state_store(target, state, "rooms")


def save_single_state(state: dict[str, Any], path: Optional[Path] = None) -> None:
    target = path or single_state_path()
    _save_state_store(target, state, "conversations")


def create_room_record(
    name: str,
    profiles: list[str],
    owner_id: str = LOCAL_OWNER_ID,
) -> dict[str, Any]:
    now = int(time.time() * 1000)
    return {
        "id": f"room_{uuid.uuid4().hex[:12]}",
        "name": name.strip() or "新群聊",
        "profiles": list(dict.fromkeys(p.strip() for p in profiles if p.strip())),
        "owner_id": str(owner_id or LOCAL_OWNER_ID).strip() or LOCAL_OWNER_ID,
        "messages": [],
        "created_at": now,
        "updated_at": now,
    }


def create_single_conversation(
    profile: str,
    title: str = "新对话",
) -> dict[str, Any]:
    now = int(time.time() * 1000)
    return {
        "id": f"chat_{uuid.uuid4().hex[:12]}",
        "title": title.strip() or "新对话",
        "profile": profile.strip() or "default",
        "runtime_sessions": {},
        "runtime_runs": {},
        "hosted_turns": {},
        "messages": [],
        "created_at": now,
        "updated_at": now,
    }


def create_adopted_single_conversation(
    profile: str,
    session_id: str,
    title: str,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    conversation = create_single_conversation(profile, title)
    set_conversation_runtime_session(conversation, profile, session_id)
    pending_assistant: list[dict[str, Any]] = []

    def flush_assistant_turn() -> None:
        if not pending_assistant:
            return
        content = next(
            (
                text
                for text in reversed(
                    [_message_content_text(item) for item in pending_assistant]
                )
                if text
            ),
            "",
        )
        activities = build_runtime_activity_timeline(pending_assistant)
        source = next(
            (
                item
                for item in reversed(pending_assistant)
                if str(item.get("role") or "").lower() == "assistant"
            ),
            pending_assistant[-1],
        )
        source_meta = source.get("meta")
        meta = dict(source_meta) if isinstance(source_meta, dict) else {}
        if activities:
            meta["activities"] = activities
        if content or activities:
            message = _append_message(
                conversation,
                role="assistant",
                name=str(source.get("name") or profile).strip() or profile,
                content=content,
                status=str(source.get("status") or "completed"),
                kind="message",
                meta=meta,
            )
            timestamp = source.get("timestamp")
            if isinstance(timestamp, (int, float)) and timestamp > 0:
                message["created_at"] = int(timestamp * 1000)
        pending_assistant.clear()

    for source in messages:
        role = str(source.get("role") or "assistant").strip().lower()
        if role == "user":
            flush_assistant_turn()
            content = _message_content_text(source)
            if not content:
                continue
            message = _append_message(
                conversation,
                role="user",
                name="user",
                content=content,
                status=str(source.get("status") or "completed"),
                kind="message",
                meta=(
                    source.get("meta")
                    if isinstance(source.get("meta"), dict)
                    else None
                ),
            )
            timestamp = source.get("timestamp")
            if isinstance(timestamp, (int, float)) and timestamp > 0:
                message["created_at"] = int(timestamp * 1000)
        elif role in {"assistant", "tool"}:
            pending_assistant.append(source)
    flush_assistant_turn()
    if conversation["messages"]:
        conversation["updated_at"] = max(
            message["created_at"]
            for message in conversation["messages"]
        )
    return conversation


def set_conversation_runtime_session(
    conversation: dict[str, Any],
    profile: str,
    session_id: str,
) -> dict[str, str]:
    profile_name = profile.strip() or "default"
    runtime_sessions = conversation.get("runtime_sessions")
    if not isinstance(runtime_sessions, dict):
        runtime_sessions = {}
        conversation["runtime_sessions"] = runtime_sessions
    normalized_session_id = session_id.strip()
    if normalized_session_id:
        runtime_sessions[profile_name] = normalized_session_id
    else:
        runtime_sessions.pop(profile_name, None)
    conversation["updated_at"] = int(time.time() * 1000)
    return runtime_sessions


def mark_conversation_runtime_run(
    conversation: dict[str, Any],
    profile: str,
    session_id: str,
    *,
    turn_id: str = "",
    baseline_message_count: int = 0,
    started_at: Optional[int] = None,
) -> dict[str, Any]:
    """Register a gateway-owned turn before its prompt is submitted.

    The record lets the dashboard process recover a completed answer from the
    profile's persistent SessionDB even when iOS has discarded the WebView.
    """
    profile_name = profile.strip() or "default"
    normalized_session_id = session_id.strip()
    set_conversation_runtime_session(
        conversation,
        profile_name,
        normalized_session_id,
    )
    runtime_runs = conversation.get("runtime_runs")
    if not isinstance(runtime_runs, dict):
        runtime_runs = {}
        conversation["runtime_runs"] = runtime_runs
    now = int(time.time() * 1000) if started_at is None else int(started_at)
    runtime_runs[profile_name] = {
        "session_id": normalized_session_id,
        "turn_id": str(turn_id).strip(),
        "status": "running",
        "baseline_message_count": max(0, int(baseline_message_count or 0)),
        "started_at": now,
        "updated_at": now,
    }
    conversation["updated_at"] = now
    return runtime_runs[profile_name]


def _load_runtime_messages(profile: str, session_id: str) -> list[dict[str, Any]]:
    from hermes_cli.profiles import get_profile_dir
    from hermes_state import SessionDB

    db_path = get_profile_dir(profile) / "state.db"
    if not db_path.exists():
        return []
    db = SessionDB(db_path=db_path, read_only=True)
    try:
        resolved = db.resolve_session_id(session_id)
        if not resolved:
            return []
        resolved = db.resolve_resume_session_id(resolved)
        return db.get_messages(resolved)
    finally:
        db.close()


def _delete_runtime_session(profile: str, session_id: str) -> bool:
    from hermes_cli.profiles import get_profile_dir
    from hermes_state import SessionDB

    normalized_session_id = session_id.strip()
    if not normalized_session_id:
        return False
    db_path = get_profile_dir(profile.strip() or "default") / "state.db"
    if not db_path.exists():
        return False
    db = SessionDB(db_path=db_path)
    try:
        resolved = db.resolve_session_id(normalized_session_id)
        return bool(resolved and db.delete_session(resolved))
    finally:
        db.close()


def _message_content_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if not isinstance(text, str):
                    text = item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part.strip() for part in parts if part.strip()).strip()
    if isinstance(content, dict):
        for key in ("text", "content", "output", "result"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        try:
            return json.dumps(content, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return ""
    return ""


_REDACTED_VALUE = "[REDACTED]"
_SENSITIVE_ACTIVITY_KEYS = {
    "authorization",
    "proxy_authorization",
    "cookie",
    "set_cookie",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "auth_token",
    "session_token",
    "token",
    "password",
    "passwd",
    "secret",
    "credential",
    "credentials",
    "private_key",
}
_INLINE_BEARER_RE = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]{8,}")
_INLINE_SECRET_RE = re.compile(
    r"(?i)\b(Authorization|Proxy-Authorization|Cookie|Set-Cookie|"
    r"X-Api-Key|Api-Key|API[_ -]?Key|Access[_ -]?Token|Refresh[_ -]?Token|"
    r"Password|Passwd|Secret|Credential)\b\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_INLINE_ENV_SECRET_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])((?:[A-Za-z][A-Za-z0-9_]*_)?(?:"
    r"API_KEY|APIKEY|ACCESS_TOKEN|REFRESH_TOKEN|AUTH_TOKEN|SESSION_TOKEN|TOKEN|"
    r"PASSWORD|PASSWD|SECRET|CREDENTIAL|CREDENTIALS|PRIVATE_KEY))"
    r"\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


def _sensitive_activity_key(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return normalized in _SENSITIVE_ACTIVITY_KEYS or any(
        normalized.endswith(suffix)
        for suffix in (
            "_api_key",
            "_access_token",
            "_refresh_token",
            "_auth_token",
            "_session_token",
            "_password",
            "_passwd",
            "_secret",
            "_credential",
            "_credentials",
            "_private_key",
        )
    )


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                _REDACTED_VALUE
                if _sensitive_activity_key(key)
                else _redact_sensitive(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive(item) for item in value]
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.startswith(("{", "[")) and stripped.endswith(("}", "]")):
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            return json.dumps(
                _redact_sensitive(parsed),
                ensure_ascii=False,
                separators=(",", ":"),
            )
    redacted = _INLINE_BEARER_RE.sub(r"\1 [REDACTED]", value)
    redacted = _INLINE_SECRET_RE.sub(r"\1: [REDACTED]", redacted)
    return _INLINE_ENV_SECRET_RE.sub(r"\1: [REDACTED]", redacted)


def _structured_text(value: Any) -> str:
    value = _redact_sensitive(value)
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return str(value).strip()
    return str(value).strip()


def sanitize_runtime_error(error: Any) -> str:
    """Return a short user-facing error without upstream HTML or proxy internals."""
    text = _structured_text(error)
    lowered = text.lower()
    status_match = re.search(r"(?:http\s*)?(4\d\d|5\d\d)", lowered)
    status = status_match.group(1) if status_match else ""
    if status == "429" or "rate limit" in lowered:
        return "模型服务请求过多（HTTP 429），已保留当前进度。"
    if status in {"500", "502", "503", "504", "520", "522", "524"} or any(
        marker in lowered
        for marker in ("bad gateway", "cloudflare", "origin web server", "upstream")
    ):
        visible_status = status or "502"
        return f"模型服务暂时繁忙（HTTP {visible_status}），已保留当前进度。"
    if any(
        marker in lowered
        for marker in ("timed out", "timeout", "connection reset", "connection aborted")
    ):
        return "模型服务连接超时，已保留当前进度。"
    without_html = re.sub(r"<[^>]+>", " ", text)
    without_html = re.sub(r"\s+", " ", without_html).strip()
    return (without_html or "Hermes 执行失败")[:400]


def _is_transient_runtime_error(error: Any) -> bool:
    text = _structured_text(error).lower()
    return bool(
        re.search(r"(?:http\s*)?(429|500|502|503|504|520|522|524)", text)
        or any(
            marker in text
            for marker in (
                "bad gateway",
                "cloudflare",
                "timed out",
                "timeout",
                "connection reset",
                "connection aborted",
                "temporarily unavailable",
            )
        )
    )


def _reasoning_text(message: dict[str, Any]) -> str:
    for key in ("reasoning_content", "reasoning", "thinking"):
        text = _structured_text(message.get(key))
        if text:
            return text
    meta = message.get("meta")
    if isinstance(meta, dict):
        return _structured_text(meta.get("reasoning"))
    return ""


def _reasoning_repeats_final(reasoning: Any, final_content: Any) -> bool:
    reasoning_text = re.sub(r"\s+", " ", _structured_text(reasoning)).strip()
    final_text = re.sub(r"\s+", " ", _structured_text(final_content)).strip()
    if not reasoning_text or not final_text:
        return False
    if reasoning_text == final_text:
        return True
    minimum_prefix = 8
    return (
        len(reasoning_text) >= minimum_prefix
        and final_text.startswith(reasoning_text)
    ) or (
        len(final_text) >= minimum_prefix
        and reasoning_text.startswith(final_text)
    )


def _tool_category(name: str) -> str:
    lowered = name.strip().lower()
    if lowered.startswith("mcp__") or lowered.startswith("mcp_"):
        return "mcp"
    if "skill" in lowered:
        return "skill"
    if any(marker in lowered for marker in ("web_search", "search_web", "browse", "browser")):
        return "web"
    if any(marker in lowered for marker in ("terminal", "shell", "command", "exec", "bash", "powershell")):
        return "command"
    if any(marker in lowered for marker in ("read_file", "write_file", "patch", "filesystem", "glob", "search_files")):
        return "file"
    if any(marker in lowered for marker in ("delegate", "subagent", "spawn_agent")):
        return "subagent"
    return "other"


def _timestamp_ms(message: dict[str, Any]) -> Optional[int]:
    value = message.get("timestamp")
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value if value > 10_000_000_000 else value * 1000)


def build_runtime_activity_timeline(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rebuild reasoning phases and tool calls from SessionDB messages."""
    activities: list[dict[str, Any]] = []
    tools_by_id: dict[str, dict[str, Any]] = {}
    sequence = 0
    previous_timestamp: Optional[int] = None

    for message in messages:
        role = str(message.get("role") or "").lower()
        message_timestamp = _timestamp_ms(message)
        if role == "assistant":
            reasoning = _reasoning_text(message)
            final_content = _message_content_text(message)
            if reasoning and not _reasoning_repeats_final(reasoning, final_content):
                sequence += 1
                activity = {
                    "id": f"reasoning-{sequence}",
                    "kind": "reasoning",
                    "category": "reasoning",
                    "name": "模型思考",
                    "input": "",
                    "output": reasoning,
                    "status": "completed",
                    "started_at": previous_timestamp,
                    "ended_at": message_timestamp,
                }
                if (
                    previous_timestamp is not None
                    and message_timestamp is not None
                    and message_timestamp >= previous_timestamp
                ):
                    activity["duration_ms"] = message_timestamp - previous_timestamp
                activities.append(activity)
            tool_calls = message.get("tool_calls")
            for call in (tool_calls if isinstance(tool_calls, list) else []):
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                function = function if isinstance(function, dict) else {}
                sequence += 1
                tool_id = str(call.get("id") or f"tool-{sequence}")
                name = str(function.get("name") or call.get("name") or "tool")
                activity = {
                    "id": tool_id,
                    "kind": "tool",
                    "category": _tool_category(name),
                    "name": name,
                    "input": _structured_text(
                        function.get("arguments", call.get("arguments"))
                    ),
                    "output": "",
                    "status": "running",
                    "started_at": message_timestamp,
                    "ended_at": None,
                }
                activities.append(activity)
                tools_by_id[tool_id] = activity
        elif role == "tool":
            tool_id = str(
                message.get("tool_call_id")
                or message.get("id")
                or ""
            )
            activity = tools_by_id.get(tool_id)
            if activity is None:
                sequence += 1
                name = str(message.get("name") or "tool")
                activity = {
                    "id": tool_id or f"tool-result-{sequence}",
                    "kind": "tool",
                    "category": _tool_category(name),
                    "name": name,
                    "input": "",
                    "output": "",
                    "status": "running",
                    "started_at": None,
                    "ended_at": None,
                }
                activities.append(activity)
            output = _message_content_text(message)
            activity["output"] = output
            activity["status"] = (
                "failed"
                if message.get("error") or str(message.get("status") or "").lower() in {"error", "failed"}
                else "completed"
            )
            activity["ended_at"] = message_timestamp
            if activity.get("started_at") and activity.get("ended_at"):
                activity["duration_ms"] = max(
                    0,
                    activity["ended_at"] - activity["started_at"],
                )

        if message_timestamp is not None:
            previous_timestamp = message_timestamp

    return activities


def normalize_stored_conversation_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fold legacy standalone tool rows into their assistant response."""
    normalized: list[dict[str, Any]] = []
    for source in messages:
        if not isinstance(source, dict):
            continue
        role = str(source.get("role") or "assistant").lower()
        if role != "tool":
            message = dict(source)
            content = _message_content_text(message)
            if content or not message.get("content"):
                message["content"] = content
            normalized.append(message)
            continue

        target = next(
            (
                item
                for item in reversed(normalized)
                if str(item.get("role") or "").lower() == "assistant"
            ),
            None,
        )
        if target is None:
            target = {
                "id": f"recovered-tool-{uuid.uuid4().hex[:10]}",
                "role": "assistant",
                "name": str(source.get("name") or "default"),
                "content": "",
                "status": str(source.get("status") or "completed"),
                "kind": "message",
                "created_at": source.get("created_at") or int(time.time() * 1000),
                "meta": {},
            }
            normalized.append(target)
        meta = target.get("meta")
        meta = dict(meta) if isinstance(meta, dict) else {}
        activities = meta.get("activities")
        activities = list(activities) if isinstance(activities, list) else []
        activities.extend(build_runtime_activity_timeline([source]))
        meta["activities"] = activities
        target["meta"] = meta
    return normalized


def _runtime_assistant_turns(
    messages: list[dict[str, Any]],
) -> list[tuple[str, dict[str, Any], list[dict[str, Any]]]]:
    turns: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
    pending: list[dict[str, Any]] = []

    def flush() -> None:
        if not pending:
            return
        assistants = [
            item
            for item in pending
            if str(item.get("role") or "").lower() == "assistant"
        ]
        source = assistants[-1] if assistants else pending[-1]
        content = next(
            (
                text
                for text in reversed(
                    [_message_content_text(item) for item in assistants]
                )
                if text
            ),
            "",
        )
        if content:
            turns.append((content, source, list(pending)))
        pending.clear()

    for message in messages:
        role = str(message.get("role") or "assistant").lower()
        if role == "user":
            flush()
        elif role in {"assistant", "tool"}:
            pending.append(message)
    flush()
    return turns


def reconcile_conversation_mapped_sessions(
    conversation: dict[str, Any],
    *,
    loader: Callable[[str, str], list[dict[str, Any]]] = _load_runtime_messages,
) -> bool:
    """Backfill assistant turns lost by legacy session-level replacement."""
    runtime_sessions = conversation.get("runtime_sessions")
    if not isinstance(runtime_sessions, dict):
        return False
    runtime_runs = conversation.get("runtime_runs")
    runtime_runs = runtime_runs if isinstance(runtime_runs, dict) else {}
    sync_counts = conversation.get("runtime_sync_counts")
    if not isinstance(sync_counts, dict):
        sync_counts = {}
        conversation["runtime_sync_counts"] = sync_counts

    changed = False
    for profile, raw_session_id in runtime_sessions.items():
        session_id = str(raw_session_id or "").strip()
        if not session_id:
            continue
        active_run = runtime_runs.get(profile)
        if (
            isinstance(active_run, dict)
            and active_run.get("status") == "running"
            and active_run.get("session_id") == session_id
        ):
            continue
        try:
            runtime_messages = loader(str(profile), session_id)
        except Exception:
            continue
        sync_key = f"{profile}:{session_id}"
        if sync_counts.get(sync_key) == len(runtime_messages):
            continue

        existing_counts: Counter[str] = Counter()
        for message in conversation.get("messages") or []:
            if str(message.get("role") or "").lower() != "assistant":
                continue
            meta = message.get("meta")
            meta = meta if isinstance(meta, dict) else {}
            belongs_to_session = meta.get("runtime_session_id") == session_id
            belongs_to_profile = (
                not meta.get("runtime_session_id")
                and str(message.get("name") or "") == str(profile)
            )
            content = _message_content_text(message)
            if content and (belongs_to_session or belongs_to_profile):
                existing_counts[content] += 1

        matched_counts: Counter[str] = Counter()
        for ordinal, (content, source, turn_messages) in enumerate(
            _runtime_assistant_turns(runtime_messages),
            start=1,
        ):
            if matched_counts[content] < existing_counts[content]:
                matched_counts[content] += 1
                continue
            recovered = _append_message(
                conversation,
                role="assistant",
                name=str(profile),
                content=content,
                status="completed",
                meta={
                    "runtime_session_id": session_id,
                    "runtime_turn_id": f"recovered:{session_id}:{ordinal}",
                    "runtime_recovered": True,
                    "activities": build_runtime_activity_timeline(turn_messages),
                },
            )
            timestamp = _timestamp_ms(source)
            if timestamp:
                recovered["created_at"] = timestamp
            changed = True

        sync_counts[sync_key] = len(runtime_messages)
        changed = True

    if changed:
        conversation["messages"] = sorted(
            conversation.get("messages") or [],
            key=lambda message: int(message.get("created_at") or 0),
        )
        if conversation["messages"]:
            conversation["updated_at"] = max(
                int(message.get("created_at") or 0)
                for message in conversation["messages"]
            )
    return changed


def reconcile_conversation_runtime_results(
    conversation: dict[str, Any],
    *,
    loader: Callable[[str, str], list[dict[str, Any]]] = _load_runtime_messages,
    now_ms: Optional[int] = None,
) -> bool:
    """Persist completed detached turns back into their original conversation."""
    runtime_runs = conversation.get("runtime_runs")
    if not isinstance(runtime_runs, dict):
        return False
    changed = False
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    for profile, run in runtime_runs.items():
        if not isinstance(run, dict) or run.get("status") != "running":
            continue
        session_id = str(run.get("session_id") or "").strip()
        if not session_id:
            continue
        try:
            messages = loader(str(profile), session_id)
        except Exception:
            continue
        baseline = max(0, int(run.get("baseline_message_count") or 0))
        candidates = [
            (_message_content_text(message), message)
            for message in messages[baseline:]
            if str(message.get("role") or "").lower() == "assistant"
        ]
        final_text = next(
            (text for text, _message in reversed(candidates) if text),
            "",
        )
        if not final_text:
            continue
        existing = next(
            (
                message
                for message in conversation.get("messages") or []
                if isinstance(message.get("meta"), dict)
                and (
                    message["meta"].get("runtime_turn_id") == run.get("turn_id")
                    if run.get("turn_id")
                    else message["meta"].get("runtime_session_id") == session_id
                )
            ),
            None,
        )
        if existing is None:
            activities = build_runtime_activity_timeline(messages[baseline:])
            recovered = _append_message(
                conversation,
                role="assistant",
                name=str(profile),
                content=final_text,
                status="completed",
                meta={
                    "runtime_session_id": session_id,
                    "runtime_turn_id": str(run.get("turn_id") or ""),
                    "recovered": True,
                    "activities": activities,
                },
            )
            recovered["created_at"] = now
        run["status"] = "completed"
        run["completed_at"] = now
        run["updated_at"] = now
        conversation["updated_at"] = now
        changed = True
    return changed


def available_profiles() -> list[dict[str, Any]]:
    return [
        {
            "name": profile.name,
            "description": profile.description or "",
            "model": profile.model or "",
            "provider": profile.provider or "",
            "gateway_running": bool(profile.gateway_running),
        }
        for profile in list_profiles()
    ]


_WORKER_TARGETS = ("dbb3", "pc")
_WORKER_TARGET_PROFILES = {
    "dbb3": "dbb3-worker",
    "pc": "pc-worker",
}


def _target_marker_pattern(markers: tuple[str, ...]) -> str:
    patterns: list[str] = []
    for marker in markers:
        if re.fullmatch(r"[a-z0-9 ]+", marker):
            words = r"\s+".join(re.escape(part) for part in marker.split())
            patterns.append(rf"(?<![a-z0-9]){words}(?![a-z0-9])")
        else:
            patterns.append(re.escape(marker))
    return "(?:" + "|".join(patterns) + ")"


def _target_constraints(content: str) -> dict[str, list[str]]:
    """Extract deterministic worker placement constraints from user wording."""
    text = re.sub(r"\s+", " ", str(content or "").strip().lower())
    marker_groups = {
        "dbb3": _DBB3_MARKERS,
        "pc": _PC_MARKERS,
    }
    mentioned: set[str] = set()
    excluded: set[str] = set()
    only: set[str] = set()
    for target, markers in marker_groups.items():
        target_pattern = _target_marker_pattern(markers)
        if re.search(target_pattern, text):
            mentioned.add(target)
        negative_patterns = (
            rf"(?:\bdo\s+not\b|\bdon['’]?t\b|\bdont\b|\bnot\b|\bnever\b|\bwithout\b|"
            rf"\bavoid\b|\bexclude\b)[^\n.!?,;。！？，；]{{0,64}}?{target_pattern}",
            rf"{target_pattern}[^\n.!?,;。！？，；]{{0,40}}?(?:\bmust\s+not\b|\bshould\s+not\b|"
            rf"\bdo\s+not\b|\bdon['’]?t\b|\bexcluded\b|\bdisabled\b)",
            rf"(?:不要|别|不应|无需|不用|不在|排除|避免)(?:在|用|使用|让|安排|运行|执行|部署|派到|交给)?"
            rf"[^,;，。；！？\n]{{0,24}}?{target_pattern}",
            rf"{target_pattern}[^,;，。；！？\n]{{0,20}}?(?:不要|别用|不运行|不执行|排除|禁用)",
        )
        if any(re.search(pattern, text) for pattern in negative_patterns):
            excluded.add(target)
        only_patterns = (
            rf"(?:\bonly\b|\bexclusively\b)[^\n.!?,;。！？，；]{{0,48}}?{target_pattern}",
            rf"{target_pattern}[^\n.!?,;。！？，；]{{0,24}}?(?:\bonly\b|\bexclusively\b)",
            rf"(?:只|仅)(?:在|用|使用|让|安排|运行|执行|部署|交给)?[^,;，。；！？\n]{{0,24}}?{target_pattern}",
            rf"{target_pattern}[^,;，。；！？\n]{{0,12}}?(?:专用|即可|就行)",
        )
        if any(re.search(pattern, text) for pattern in only_patterns):
            only.add(target)

    if only:
        excluded.update(set(_WORKER_TARGETS) - only)
        mentioned.update(only)
    included = mentioned - excluded
    return {
        "included": [target for target in _WORKER_TARGETS if target in included],
        "excluded": [target for target in _WORKER_TARGETS if target in excluded],
        "only": [target for target in _WORKER_TARGETS if target in only],
    }


def _constrained_worker_profiles(
    content: str,
    *,
    profiles: Optional[list[str]] = None,
    targets: Optional[list[str]] = None,
) -> tuple[list[str], dict[str, list[str]]]:
    constraints = _target_constraints(content)
    selected_targets: list[str] = []
    for profile in profiles or []:
        normalized = str(profile).strip().lower()
        if normalized in _WORKER_TARGET_PROFILES.values():
            target = normalized.removesuffix("-worker")
            if target not in selected_targets:
                selected_targets.append(target)
    for target in targets or []:
        normalized = str(target).strip().lower()
        if normalized in _WORKER_TARGETS and normalized not in selected_targets:
            selected_targets.append(normalized)

    excluded = set(constraints["excluded"])
    only = set(constraints["only"])
    if only:
        selected_targets = [target for target in _WORKER_TARGETS if target in only]
    else:
        selected_targets = [target for target in selected_targets if target not in excluded]
        for target in constraints["included"]:
            if target not in selected_targets:
                selected_targets.append(target)
        if not selected_targets and excluded:
            selected_targets = [
                target for target in _WORKER_TARGETS if target not in excluded
            ]
    if not selected_targets and excluded.issuperset(_WORKER_TARGETS):
        return [], constraints
    if not selected_targets:
        selected_targets = ["dbb3"]
    return [
        _WORKER_TARGET_PROFILES[target]
        for target in selected_targets
        if target in _WORKER_TARGET_PROFILES
    ], constraints


def _work_profiles(lowered: str) -> list[str]:
    worker_profiles, _constraints = _constrained_worker_profiles(lowered)
    return ["default", *worker_profiles, "reviewer"]


def _contains_intent_marker(text: str, marker: str) -> bool:
    if re.fullmatch(r"[a-z0-9 ]+", marker):
        words = r"\s+".join(re.escape(part) for part in marker.split())
        return bool(re.search(rf"(?<![a-z0-9]){words}(?![a-z0-9])", text))
    return marker in text


def requires_artifact_delivery(content: str) -> bool:
    """Return true only when the user explicitly asks for a file deliverable."""
    lowered = re.sub(r"\s+", " ", str(content or "").strip().lower())
    for clause in re.split(r"[\n.!?;。！？；]+", lowered):
        if not any(
            _contains_intent_marker(clause, marker)
            for marker in (*_DIRECT_ARTIFACT_MARKERS, *_ARTIFACT_NOUN_MARKERS)
        ):
            continue
        for marker in _ARTIFACT_ACTION_MARKERS:
            pattern = _target_marker_pattern((marker,))
            for match in re.finditer(pattern, clause):
                prefix = clause[max(0, match.start() - 64):match.start()]
                english_negated = re.search(
                    r"(?:\bdo\s+not\b|\bdon['’]?t\b|\bdont\b|\bnever\b|"
                    r"\bwithout\b|\bavoid\b)[^.;。！？；]{0,64}$",
                    prefix,
                )
                chinese_negated = re.search(
                    r"(?:不要|别|无需|不用|避免)[^.;。！？；]{0,40}$",
                    prefix,
                )
                if english_negated is None and chinese_negated is None:
                    return True
    return False


def collaboration_role(profile: str) -> str:
    normalized = str(profile or "").strip().lower()
    if normalized == "reviewer" or "review" in normalized:
        return "reviewer"
    if normalized.endswith("worker") or "worker" in normalized:
        return "worker"
    return "reporter"


def collaboration_execution_order(profiles: list[str]) -> list[str]:
    """Order one worker, reviewer, then exactly one final reporter."""
    selected = list(dict.fromkeys(str(item).strip() for item in profiles if str(item).strip()))
    if not selected:
        return []
    reporter = next(
        (item for item in selected if collaboration_role(item) == "reporter"),
        selected[0],
    )
    workers = [
        item
        for item in selected
        if item != reporter and collaboration_role(item) == "worker"
    ]
    reviewers = [
        item
        for item in selected
        if item != reporter and collaboration_role(item) == "reviewer"
    ]
    return [*workers, *reviewers, reporter]


def _rule_based_user_intent(content: str) -> dict[str, Any]:
    text = content.strip()
    lowered = text.lower()
    title = summarize_task_title(text)
    matched = [marker for marker in _WORK_MARKERS if marker in lowered]
    complex_matches = [
        marker for marker in _COMPLEX_WORK_MARKERS if marker in lowered
    ]
    device_matches = [
        marker
        for marker in (*_PC_MARKERS, *_DBB3_MARKERS)
        if marker in lowered
    ]
    multi_step_count = sum(
        lowered.count(marker) for marker in _MULTI_STEP_MARKERS
    )
    score = min(6, len(complex_matches) * 2)
    score += min(3, len(device_matches) * 2)
    score += min(3, multi_step_count)
    score += 1 if matched else 0
    score += 2 if len(text) >= 80 else 0
    if any(marker in lowered for marker in _SIMPLE_CHAT_MARKERS) and len(text) < 30:
        score -= 3

    if score < 4:
        if any(marker in lowered for marker in _SIMPLE_CHAT_MARKERS):
            confidence = 0.96
        elif not matched and len(text) <= 12:
            confidence = 0.62
        elif score <= 0:
            confidence = 0.82
        else:
            confidence = 0.68
        return {
            "mode": "chat",
            "label": "简单任务",
            "title": title,
            "reason": "当前请求可由一个 Hermes 直接回答或完成，不需要创建群聊工作流。",
            "confidence": confidence,
            "source": "rules",
            "profiles": ["default"],
            "artifact_required": requires_artifact_delivery(text),
        }

    profiles = _work_profiles(lowered)
    return {
        "mode": "work",
        "label": "群聊 + 工作流",
        "title": title,
        "reason": "检测到多步骤、设备操作、代码修改或交付要求，将创建工作流并启动多 Profile 协作。",
        "confidence": 0.95 if score >= 7 else 0.86,
        "source": "rules",
        "profiles": profiles,
        "targets": [
            profile.removesuffix("-worker")
            for profile in profiles
            if profile in _WORKER_TARGET_PROFILES.values()
        ],
        "target_constraints": _target_constraints(lowered),
        "artifact_required": requires_artifact_delivery(text),
    }


def classify_intent_with_model(content: str) -> Optional[dict[str, Any]]:
    """Ask the configured auxiliary model only for ambiguous routing cases."""
    from agent.auxiliary_client import call_llm, extract_content_or_reasoning

    response = call_llm(
        task="intent_routing",
        messages=[
            {
                "role": "system",
                "content": (
                    "你是 Hermes 任务路由器。判断用户请求应走 simple 还是 workflow。"
                    "simple 表示普通聊天、问答、总结、搜索或一个 Hermes 可直接完成的单步任务；"
                    "workflow 表示多步骤执行、设备协作、修改代码、部署、测试、生成交付文件或长期任务。"
                    "只输出 JSON：{\"mode\":\"chat|work\",\"confidence\":0到1,"
                    "\"reason\":\"一句中文理由\"}。"
                ),
            },
            {"role": "user", "content": content.strip()},
        ],
        temperature=0,
        max_tokens=160,
        timeout=15,
    )
    raw = extract_content_or_reasoning(response)
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        return None
    parsed = json.loads(match.group(0))
    mode = str(parsed.get("mode") or "").strip().lower()
    if mode not in {"chat", "work"}:
        return None
    return {
        "mode": mode,
        "confidence": max(
            0.0,
            min(1.0, float(parsed.get("confidence") or 0.75)),
        ),
        "reason": str(
            parsed.get("reason") or "模型根据任务复杂度完成判断。"
        )[:180],
    }


def classify_intent_with_context_model(
    content: str,
    *,
    recent_messages: Optional[list[dict[str, Any]]] = None,
    attachments: Optional[list[dict[str, Any]]] = None,
    adjudicate: bool = False,
) -> Optional[dict[str, Any]]:
    """Model-first structured routing for native hosted conversations."""
    from agent.auxiliary_client import call_llm, extract_content_or_reasoning

    context = {
        "current_message": str(content or "").strip()[:12_000],
        "recent_messages": [
            {
                "role": str(item.get("role") or ""),
                "content": str(item.get("content") or "")[:2_000],
            }
            for item in (recent_messages or [])[-12:]
            if isinstance(item, dict)
        ],
        "attachments": [
            {
                "name": str(item.get("name") or item.get("filename") or "")[:240],
                "mime_type": str(item.get("mime_type") or item.get("type") or "")[:120],
                "source": str(item.get("source") or "user")[:40],
            }
            for item in (attachments or [])[:32]
            if isinstance(item, dict)
        ],
    }
    system = (
        "Classify this Hermes conversation turn from semantics and context. "
        "chat is ordinary conversation, explanation, translation, summary, search, or read-only analysis that one "
        "Hermes can answer. work is concrete development/operations, tool execution, state mutation, deployment, "
        "multi-step execution, or creating a deliverable. An uploaded file is normally input, not a requested output. "
        "Repository edits do not imply a downloadable artifact. Resolve references such as continue or send that file "
        "from recent_messages. Select targets from dbb3 and pc (pc includes Windows, WSL, and the local computer). "
        "Return JSON only: {mode:'chat|work',needs_execution:boolean,needs_tools:boolean,mutates_state:boolean,"
        "targets:[],profiles:[],artifact:{decision:'required|optional|none',types:[],"
        "producer_targets:[],producer_profiles:[],reason:string},"
        "confidence:0..1,reason:string}. Work profiles are dbb3-worker and pc-worker."
    )
    if adjudicate:
        system += " This is a second adjudication for a low-confidence first result; resolve the boundary explicitly."
    response = call_llm(
        task="intent_routing",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        temperature=0,
        max_tokens=900,
        timeout=30,
    )
    raw = extract_content_or_reasoning(response)
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        return None
    parsed = json.loads(match.group(0))
    mode = str(parsed.get("mode") or "").strip().lower()
    if mode not in {"chat", "work"}:
        return None
    targets = []
    for value in parsed.get("targets") or []:
        normalized = str(value).strip().lower()
        if normalized in {"server", "remote", "linux"}:
            normalized = "dbb3"
        elif normalized in {"windows", "wsl", "local"}:
            normalized = "pc"
        if normalized in {"dbb3", "pc"} and normalized not in targets:
            targets.append(normalized)
    profiles = [
        str(value).strip().lower()
        for value in parsed.get("profiles") or []
        if str(value).strip().lower() in {"dbb3-worker", "pc-worker"}
    ]
    if mode == "work" and not profiles:
        profiles = [f"{target}-worker" for target in targets] or ["dbb3-worker"]
    artifact = parsed.get("artifact") if isinstance(parsed.get("artifact"), dict) else {}
    artifact_decision = str(artifact.get("decision") or "none").strip().lower()
    if artifact_decision not in {"required", "optional", "none"}:
        artifact_decision = "none"
    return {
        "mode": mode,
        "needs_execution": bool(parsed.get("needs_execution", mode == "work")),
        "needs_tools": bool(parsed.get("needs_tools", mode == "work")),
        "mutates_state": bool(parsed.get("mutates_state", mode == "work")),
        "targets": targets,
        "profiles": profiles,
        "artifact": {
            "decision": artifact_decision,
            "types": [str(value).strip().lower() for value in artifact.get("types") or [] if str(value).strip()],
            "producer_targets": [
                str(value).strip().lower()
                for value in artifact.get("producer_targets") or []
                if str(value).strip().lower() in {"dbb3", "pc"}
            ],
            "producer_profiles": [
                str(value).strip().lower()
                for value in artifact.get("producer_profiles") or []
                if str(value).strip().lower() in {"dbb3-worker", "pc-worker"}
            ],
            "reason": str(artifact.get("reason") or "")[:500],
        },
        "confidence": max(0.0, min(1.0, float(parsed.get("confidence") or 0.0))),
        "reason": str(parsed.get("reason") or "Model semantic routing")[:500],
    }


def classify_user_intent(
    content: str,
    *,
    model_classifier: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    routed = _rule_based_user_intent(content)
    if model_classifier is None:
        return routed
    try:
        model_result = model_classifier(content.strip())
    except Exception:
        return routed
    if not isinstance(model_result, dict):
        return routed
    mode = str(model_result.get("mode") or "").strip().lower()
    if mode not in {"chat", "work"}:
        return routed
    try:
        model_confidence = max(
            0.0,
            min(1.0, float(model_result.get("confidence") or 0.0)),
        )
    except (TypeError, ValueError):
        model_confidence = 0.0
    if model_confidence < 0.70:
        try:
            second = model_classifier(content.strip())
        except Exception:
            second = None
        if isinstance(second, dict):
            try:
                second_confidence = max(
                    0.0,
                    min(1.0, float(second.get("confidence") or 0.0)),
                )
            except (TypeError, ValueError):
                second_confidence = 0.0
            second_mode = str(second.get("mode") or "").strip().lower()
            if second_mode in {"chat", "work"} and second_confidence > model_confidence:
                model_result = second
                model_confidence = second_confidence
                mode = second_mode
    model_profiles = [
        str(item).strip()
        for item in model_result.get("profiles") or []
        if str(item).strip()
    ]
    artifact = (
        dict(model_result.get("artifact"))
        if isinstance(model_result.get("artifact"), dict)
        else {
            "decision": "required" if routed.get("artifact_required") else "none",
            "types": [],
            "reason": "",
        }
    )
    artifact_decision = str(artifact.get("decision") or "none").strip().lower()
    if artifact_decision not in {"required", "optional", "none"}:
        artifact_decision = "none"
    worker_profiles, target_constraints = _constrained_worker_profiles(
        content,
        profiles=model_profiles,
        targets=list(model_result.get("targets") or []),
    )
    selected_profiles = (
        ["default"]
        if mode == "chat"
        else ["default", *worker_profiles, "reviewer"]
    )
    routed.update(
        {
            "mode": mode,
            "label": "群聊 + 工作流" if mode == "work" else "简单任务",
            "reason": str(
                model_result.get("reason") or "模型根据任务复杂度完成判断。"
            )[:180],
            "confidence": model_confidence,
            "source": "model",
            "profiles": selected_profiles,
            "targets": [
                profile.removesuffix("-worker") for profile in worker_profiles
            ],
            "target_constraints": target_constraints,
            "needs_execution": bool(model_result.get("needs_execution", mode == "work")),
            "needs_tools": bool(model_result.get("needs_tools", mode == "work")),
            "mutates_state": bool(model_result.get("mutates_state", mode == "work")),
            "artifact": {**artifact, "decision": artifact_decision},
            "artifact_required": artifact_decision == "required",
        }
    )
    return routed


def _message_line(message: dict[str, Any]) -> str:
    name = str(message.get("name") or message.get("role") or "成员").strip()
    content = str(message.get("content") or "").strip()
    return f"{name}: {content}"


def build_group_prompt(
    room: dict[str, Any],
    profile: str,
    user_message: str,
    *,
    artifact_required: bool = False,
) -> str:
    history = room.get("messages") if isinstance(room.get("messages"), list) else []
    recent = "\n".join(_message_line(item) for item in history[-_PROMPT_HISTORY:])
    members = "、".join(str(item) for item in room.get("profiles") or [])
    role = collaboration_role(profile)
    role_instruction = {
        "worker": (
            "你是执行者。只负责实际执行、调用工具并提交证据、结果和遗留问题；"
            "不要向用户做最终总结，也不要替审阅者下结论。"
        ),
        "reviewer": (
            "你是审阅者。基于执行者已经提交的结果做验收、风险检查和通过/退回判断；"
            "不要重复执行者的工作，不要向用户做最终总结。"
        ),
        "reporter": (
            "你是唯一最终汇报者。综合执行者和审阅者的信息，向用户给出一次清晰的最终结论、"
            "完成状态、关键证据、问题和下一步；不要重新执行已经完成的工作。"
        ),
    }[role]
    if artifact_required:
        artifact_instruction = (
            "用户明确要求文件交付。只有执行者可以创建所需的最终文件；审阅者只核验，"
            "最终汇报者只引用执行者产物，不得重复生成同一文件。"
        )
    else:
        artifact_instruction = (
            "本任务没有文件交付要求。不得创建或上传交付文件，直接在会话中报告文字结果。"
        )
    return (
        "你正在 Hermes 官方 WebUI 的多智能体群聊中。\n"
        f"群聊名称：{room.get('name') or '群聊'}\n"
        f"当前身份：{profile}\n"
        f"参与 Profiles：{members}\n"
        f"{role_instruction}\n"
        f"{artifact_instruction}\n"
        "请使用简体中文，避免机械重复其他成员。\n\n"
        f"最近讨论：\n{recent or '暂无'}\n\n"
        f"用户的新消息：\n{user_message.strip()}"
    )


def build_single_prompt(
    conversation: dict[str, Any],
    profile: str,
    user_message: str,
    *,
    include_projected_history: bool = True,
) -> str:
    history = (
        conversation.get("messages")
        if isinstance(conversation.get("messages"), list)
        else []
    )
    recent = (
        "\n".join(_message_line(item) for item in history[-_PROMPT_HISTORY:])
        if include_projected_history
        else ""
    )
    return (
        "你正在 Hermes 官方 WebUI 单聊中。\n"
        f"当前 Hermes Profile：{profile}\n"
        "请使用简体中文直接回答并执行用户请求。你仍可使用该 Profile 已配置的"
        "模型、Skill、MCP、记忆和工具。回复应清晰说明结果、关键过程与错误。\n\n"
        f"最近对话：\n{recent or '暂无'}\n\n"
        f"用户的新消息：\n{user_message.strip()}"
    )


def consume_profile_event_stream(
    lines: Any,
    event_callback: Optional[Callable[[dict[str, Any]], None]] = None,
) -> str:
    """Consume the profile runner's JSONL protocol without parsing display logs."""
    final_response = ""
    for raw_line in lines:
        line = str(raw_line or "").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            event["payload"] = {}
            payload = event["payload"]
        if event_callback is not None:
            event_callback(event)
        if event["type"] == "message.complete":
            final_response = _structured_text(payload.get("text"))
            if str(payload.get("status") or "").lower() == "error":
                raise RuntimeError(
                    sanitize_runtime_error(final_response or "Hermes 执行失败")
                )
        elif event["type"] == "error":
            raise RuntimeError(sanitize_runtime_error(payload.get("message")))
    return final_response


def _legacy_profile_turn(
    profile: str,
    prompt: str,
    *,
    runner: Callable[..., Any],
    hermes_bin: str,
    kanban_task_id: Optional[str] = None,
) -> str:
    command = [
        hermes_bin,
        "-p",
        profile,
        "chat",
        "-Q",
        "-q",
        prompt,
        "--source",
        "dashboard-group",
        "--max-turns",
        "45",
    ]
    env = {**os.environ, "HOME": os.environ.get("HOME", "/home/hermes")}
    if kanban_task_id:
        env["HERMES_KANBAN_TASK"] = kanban_task_id
    else:
        env.pop("HERMES_KANBAN_TASK", None)
    result = runner(
        command,
        shell=False,
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    if result.returncode != 0:
        error = result.stderr or result.stdout or "Hermes profile execution failed"
        raise RuntimeError(sanitize_runtime_error(error))
    response = (result.stdout or "").strip()
    if not response:
        raise RuntimeError("Hermes profile returned an empty response")
    return response


def run_profile_turn(
    profile: str,
    prompt: str,
    *,
    runner: Optional[Callable[..., Any]] = None,
    hermes_bin: str = "/usr/local/bin/hermes",
    event_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    process_factory: Callable[..., Any] = subprocess.Popen,
    timeout: float = 600,
    kanban_task_id: Optional[str] = None,
    session_id: str = "",
) -> str:
    """Run a profile through a structured JSONL child event channel."""
    if runner is not None:
        return _legacy_profile_turn(
            profile,
            prompt,
            runner=runner,
            hermes_bin=hermes_bin,
            kanban_task_id=kanban_task_id,
        )

    from hermes_cli.profiles import resolve_profile_env

    env = {
        **os.environ,
        "HOME": os.environ.get("HOME", "/home/hermes"),
        "HERMES_HOME": resolve_profile_env(profile),
        "HERMES_SESSION_SOURCE": "dashboard-group",
    }
    if kanban_task_id:
        env["HERMES_KANBAN_TASK"] = kanban_task_id
    else:
        env.pop("HERMES_KANBAN_TASK", None)
    command = [sys.executable, str(Path(__file__).resolve()), "--profile-event-runner"]
    process = process_factory(
        command,
        shell=False,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env=env,
    )
    if process.stdin is None or process.stdout is None:
        process.kill()
        raise RuntimeError("Hermes 结构化执行通道启动失败")
    process.stdin.write(
        json.dumps(
            {"prompt": prompt, "session_id": str(session_id or "").strip()},
            ensure_ascii=False,
        )
    )
    process.stdin.close()

    line_queue: queue.Queue[Optional[str]] = queue.Queue()

    def _read_stdout() -> None:
        try:
            for line in process.stdout:
                line_queue.put(line)
        finally:
            line_queue.put(None)

    reader = threading.Thread(target=_read_stdout, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout

    def _iter_lines():
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Hermes profile execution timed out")
            try:
                line = line_queue.get(timeout=min(1.0, remaining))
            except queue.Empty:
                if process.poll() is not None and line_queue.empty():
                    return
                continue
            if line is None:
                return
            yield line

    try:
        response = consume_profile_event_stream(_iter_lines(), event_callback)
        remaining = max(0.1, deadline - time.monotonic())
        return_code = process.wait(timeout=remaining)
    except Exception:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        raise
    stderr = process.stderr.read().strip() if process.stderr is not None else ""
    if return_code != 0:
        raise RuntimeError(sanitize_runtime_error(stderr or "Hermes profile execution failed"))
    if not response:
        raise RuntimeError("Hermes profile returned an empty response")
    return response


def run_single_turn(
    profile: str,
    prompt: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
    hermes_bin: str = "/usr/local/bin/hermes",
) -> str:
    command = [
        hermes_bin,
        "-p",
        profile,
        "chat",
        "-Q",
        "-q",
        prompt,
        "--source",
        "dashboard-single",
        "--max-turns",
        "45",
    ]
    result = runner(
        command,
        shell=False,
        capture_output=True,
        text=True,
        timeout=600,
        env={**os.environ, "HOME": os.environ.get("HOME", "/home/hermes")},
    )
    if result.returncode != 0:
        error = (
            result.stderr
            or result.stdout
            or "Hermes profile execution failed"
        ).strip()
        raise RuntimeError(error[-2000:])
    response = (result.stdout or "").strip()
    if not response:
        raise RuntimeError("Hermes profile returned an empty response")
    return response


def _append_event_delta(current: str, delta: Any) -> str:
    text = str(delta or "")
    if not text:
        return current
    if current and text.startswith(current):
        return text
    return current + text


def _activity_by_id_or_name(
    activities: list[dict[str, Any]],
    activity_id: str,
    name: str,
) -> Optional[dict[str, Any]]:
    if activity_id:
        for activity in reversed(activities):
            if str(activity.get("id") or "") == activity_id:
                return activity
    for activity in reversed(activities):
        if activity.get("status") == "running" and activity.get("name") == name:
            return activity
    return None


def _new_activity_id(prefix: str, activities: list[dict[str, Any]]) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{len(activities) + 1}"


def apply_profile_event(
    state: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    """Reduce one structured Hermes event into a persistable role snapshot."""
    event_type = str(event.get("type") or "")
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    activities = state.setdefault("activities", [])
    now = int(time.time() * 1000)

    if event_type == "session.info":
        state["runtime_session_id"] = str(
            payload.get("session_id") or state.get("runtime_session_id") or ""
        ).strip()
        state["actual_model"] = str(payload.get("model") or state.get("actual_model") or "")
        state["actual_provider"] = str(
            payload.get("provider") or state.get("actual_provider") or ""
        )
    elif event_type in {"reasoning.delta", "reasoning.available"}:
        text = _structured_text(payload.get("text"))
        if text:
            activity = next(
                (
                    item
                    for item in reversed(activities)
                    if item.get("kind") == "reasoning" and item.get("status") == "running"
                ),
                None,
            )
            if activity is None:
                activity = {
                    "id": _new_activity_id("reasoning", activities),
                    "kind": "reasoning",
                    "category": "reasoning",
                    "name": "模型思考",
                    "input": "",
                    "output": "",
                    "status": "running",
                    "started_at": payload.get("started_at") or now,
                    "ended_at": None,
                }
                activities.append(activity)
            activity["output"] = _append_event_delta(
                str(activity.get("output") or ""), text
            )[-20_000:]
            if event_type == "reasoning.available":
                activity["status"] = "completed"
                activity["ended_at"] = payload.get("ended_at") or now
                activity["duration_ms"] = max(
                    0,
                    int(activity["ended_at"]) - int(activity["started_at"]),
                )
    elif event_type == "message.delta":
        for activity in reversed(activities):
            if activity.get("kind") == "reasoning" and activity.get("status") == "running":
                activity["status"] = "completed"
                activity["ended_at"] = now
                activity["duration_ms"] = max(
                    0,
                    now - int(activity.get("started_at") or now),
                )
                break
        state["content"] = _append_event_delta(
            str(state.get("content") or ""), payload.get("text")
        )
    elif event_type in {"tool.generating", "tool.progress"}:
        name = str(payload.get("name") or "工具调用")
        activity = _activity_by_id_or_name(
            activities,
            str(payload.get("tool_id") or ""),
            name,
        )
        if activity is None and event_type == "tool.generating":
            state["updated_at"] = now
            return state
        if activity is None:
            activity = {
                "id": str(payload.get("tool_id") or _new_activity_id("tool", activities)),
                "kind": "tool",
                "category": _tool_category(name),
                "name": name,
                "input": "",
                "output": "",
                "status": "running",
                "started_at": now,
                "ended_at": None,
            }
            activities.append(activity)
        activity["preview"] = _structured_text(
            payload.get("preview") or payload.get("message")
        )[:1000]
    elif event_type == "tool.start":
        name = str(payload.get("name") or "工具调用")
        activity_id = str(payload.get("tool_id") or "")
        activity = _activity_by_id_or_name(activities, activity_id, name)
        if activity is None:
            activity = {
                "id": activity_id or _new_activity_id("tool", activities),
                "kind": "tool",
                "category": _tool_category(name),
                "name": name,
                "input": "",
                "output": "",
                "status": "running",
                "started_at": payload.get("started_at") or now,
                "ended_at": None,
            }
            activities.append(activity)
        activity.update(
            {
                "id": activity_id or activity.get("id"),
                "category": _tool_category(name),
                "name": name,
                "input": _structured_text(
                    payload.get("args") or payload.get("input") or payload.get("context")
                )[:8000],
                "status": "running",
                "started_at": activity.get("started_at") or payload.get("started_at") or now,
                "ended_at": None,
            }
        )
    elif event_type == "tool.complete":
        name = str(payload.get("name") or "工具调用")
        activity_id = str(payload.get("tool_id") or "")
        activity = _activity_by_id_or_name(activities, activity_id, name)
        if activity is None:
            activity = {
                "id": activity_id or _new_activity_id("tool", activities),
                "kind": "tool",
                "category": _tool_category(name),
                "name": name,
                "input": _structured_text(payload.get("args"))[:8000],
                "started_at": payload.get("started_at") or now,
            }
            activities.append(activity)
        error = _structured_text(payload.get("error"))
        activity.update(
            {
                "output": _structured_text(
                    payload.get("result_text")
                    or payload.get("result")
                    or payload.get("summary")
                )[-20_000:],
                "preview": _structured_text(payload.get("summary"))[:1000],
                "error": sanitize_runtime_error(error) if error else "",
                "status": "failed" if error else "completed",
                "ended_at": payload.get("ended_at") or now,
            }
        )
        if isinstance(payload.get("duration_s"), (int, float)):
            activity["duration_ms"] = round(float(payload["duration_s"]) * 1000)
        elif activity.get("started_at"):
            activity["duration_ms"] = max(
                0,
                int(activity["ended_at"]) - int(activity["started_at"]),
            )
    elif event_type.startswith("subagent."):
        name = str(payload.get("name") or payload.get("model") or "子 Agent")
        activity_id = str(payload.get("subagent_id") or "")
        activity = _activity_by_id_or_name(activities, activity_id, name)
        if activity is None:
            activity = {
                "id": activity_id or _new_activity_id("subagent", activities),
                "kind": "subagent",
                "category": "subagent",
                "name": name,
                "input": _structured_text(payload.get("goal") or payload.get("args"))[:8000],
                "output": "",
                "status": "running",
                "started_at": now,
                "ended_at": None,
            }
            activities.append(activity)
        text = _structured_text(
            payload.get("text") or payload.get("preview") or payload.get("summary")
        )
        if event_type == "subagent.complete":
            activity.update(
                {
                    "output": text[-20_000:],
                    "status": "failed"
                    if str(payload.get("status") or "").lower() in {"failed", "error"}
                    else "completed",
                    "ended_at": now,
                    "duration_ms": round(float(payload.get("duration_seconds") or 0) * 1000),
                }
            )
        else:
            activity["preview"] = text[:1000]
    elif event_type == "status.update":
        text = _structured_text(payload.get("text") or payload.get("status"))
        if text:
            activities.append(
                {
                    "id": _new_activity_id("status", activities),
                    "kind": "status",
                    "category": "other",
                    "name": "运行状态",
                    "output": text[:4000],
                    "status": "completed",
                    "started_at": now,
                    "ended_at": now,
                    "duration_ms": 0,
                }
            )
    elif event_type == "connection.retry":
        activities.append(
            {
                "id": _new_activity_id("retry", activities),
                "kind": "status",
                "category": "other",
                "name": "模型服务重试",
                "output": sanitize_runtime_error(payload.get("message")),
                "status": "completed",
                "started_at": now,
                "ended_at": now,
                "duration_ms": 0,
            }
        )
    elif event_type == "message.complete":
        final_text = _structured_text(payload.get("text"))
        if final_text:
            state["content"] = final_text
        state["runtime_session_id"] = str(
            payload.get("session_id") or state.get("runtime_session_id") or ""
        ).strip()
        state["status"] = (
            "failed" if str(payload.get("status") or "").lower() == "error" else "completed"
        )
    elif event_type == "error":
        state["error"] = sanitize_runtime_error(payload.get("message"))
        state["status"] = "failed"

    state["updated_at"] = now
    return state


def _remove_duplicate_reasoning_activities(
    activities: list[dict[str, Any]],
    final_content: str,
) -> list[dict[str, Any]]:
    if not _structured_text(final_content):
        return activities
    return [
        activity
        for activity in activities
        if not (
            activity.get("kind") == "reasoning"
            and _reasoning_repeats_final(activity.get("output"), final_content)
        )
    ]


def _runner_supports_keyword(runner: Callable[..., Any], keyword: str) -> bool:
    try:
        parameters = inspect.signature(runner).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == keyword
        or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _runner_supports_events(runner: Callable[..., Any]) -> bool:
    return _runner_supports_keyword(runner, "event_callback")


def _invoke_profile_runner(
    runner: Callable[..., str],
    profile: str,
    prompt: str,
    event_callback: Callable[[dict[str, Any]], None],
    kanban_task_id: str,
    session_id: str = "",
) -> str:
    kwargs: dict[str, Any] = {}
    if _runner_supports_events(runner):
        kwargs["event_callback"] = event_callback
    if kanban_task_id and _runner_supports_keyword(runner, "kanban_task_id"):
        kwargs["kanban_task_id"] = kanban_task_id
    if session_id and _runner_supports_keyword(runner, "session_id"):
        kwargs["session_id"] = session_id
    return str(runner(profile, prompt, **kwargs))


def _persist_hosted_role_state(
    conversation_id: str,
    turn_id: str,
    *,
    profile: str,
    role_stage: str,
    role_label: str,
    state: dict[str, Any],
    content_fallback: str,
    final_report: bool = False,
    semantic_milestone: str = "",
) -> None:
    state_content = str(state.get("content") or "").strip()
    content = state_content or content_fallback
    activities = [
        _redact_sensitive(dict(item))
        for item in state.get("activities") or []
        if isinstance(item, dict)
    ]
    base_stage = role_stage.split(":", 1)[0]
    handoff_to = {
        "worker": ["reviewer"],
        "reviewer": ["reporter"],
        "dispatch": ["worker"],
    }.get(base_stage, [])
    now = int(time.time() * 1000)
    state_status = str(state.get("status") or "streaming")
    if state_status in _HOSTED_TERMINAL_STATUSES:
        phase = "handoff" if base_stage in {"worker", "reviewer"} else "completed"
    elif state_content or activities:
        phase = "progress"
    else:
        phase = "opening"
    if semantic_milestone and state_status not in _HOSTED_TERMINAL_STATUSES:
        phase = "milestone"
        semantic_hash = hashlib.sha256(
            semantic_milestone.strip().encode("utf-8")
        ).hexdigest()[:16]
        phase_role_stage = f"{role_stage}.milestone.{semantic_hash}"
        message_key = f"{turn_id}:{role_stage}:milestone:{semantic_hash}"
    else:
        phase_role_stage = (
            role_stage
            if state_status in _HOSTED_TERMINAL_STATUSES
            else f"{role_stage}.{phase}"
        )
        message_key = f"{turn_id}:{role_stage}:{phase}"
    snapshot = {
        "profile": profile,
        "content": content,
        "status": state_status,
        "activities": activities,
        "actual_model": str(state.get("actual_model") or ""),
        "actual_provider": str(state.get("actual_provider") or ""),
        "runtime_session_id": str(
            state.get("runtime_session_id") or ""
        ).strip(),
        "started_at": int(state.get("started_at") or now),
        "completed_at": now if str(state.get("status") or "") in _HOSTED_TERMINAL_STATUSES else None,
        "milestone_count": int(state.get("milestone_count") or 0),
        "milestone_content": str(state.get("milestone_content") or ""),
        "updated_at": now,
    }
    _persist_hosted_turn(
        conversation_id,
        turn_id,
        patch={"role_events": {role_stage: snapshot}},
        runtime_session=(
            profile,
            str(state.get("runtime_session_id") or "").strip(),
        ),
        message={
            "role": "assistant",
            "name": profile,
            "content": content,
            "status": snapshot["status"],
            "kind": "message",
            "meta": {
                "role_stage": phase_role_stage,
                "base_role_stage": base_stage,
                "phase": phase,
                "message_key": message_key,
                "role_label": role_label,
                "profile": profile,
                "handoff_to": handoff_to,
                "started_at": snapshot["started_at"],
                "completed_at": snapshot["completed_at"],
                "collapse_activities": True,
                "final_report": final_report and state_status in _HOSTED_TERMINAL_STATUSES,
                "activities": activities,
                "actual_model": snapshot["actual_model"],
                "actual_provider": snapshot["actual_provider"],
                "runtime_session_id": str(
                    state.get("runtime_session_id") or ""
                ).strip(),
            },
        },
    )


def _run_hosted_role(
    conversation_id: str,
    turn_id: str,
    *,
    profile: str,
    role_stage: str,
    role_label: str,
    prompt: str,
    runner: Callable[..., str],
    kanban_task_id: str,
    start_text: str,
    runtime_profile: str = "",
    runtime_session_id: str = "",
    final_report: bool = False,
    previous_state: Optional[dict[str, Any]] = None,
) -> tuple[str, str, dict[str, Any]]:
    state = {
        "content": "",
        "status": "streaming",
        "activities": [],
        "actual_model": "",
        "actual_provider": "",
        "runtime_session_id": str(runtime_session_id or "").strip(),
        "started_at": int(time.time() * 1000),
        "milestone_count": 0,
        "milestone_content": "",
    }
    if isinstance(previous_state, dict):
        state.update(
            {
                "content": str(previous_state.get("content") or ""),
                "activities": [dict(item) for item in previous_state.get("activities") or []],
                "actual_model": str(previous_state.get("actual_model") or ""),
                "actual_provider": str(previous_state.get("actual_provider") or ""),
                "runtime_session_id": str(
                    previous_state.get("runtime_session_id")
                    or runtime_session_id
                    or ""
                ).strip(),
                "status": str(previous_state.get("status") or "streaming"),
                "started_at": int(
                    previous_state.get("started_at")
                    or state["started_at"]
                ),
                "milestone_count": int(previous_state.get("milestone_count") or 0),
                "milestone_content": str(previous_state.get("milestone_content") or ""),
            }
        )
    if (
        str(state.get("status") or "") in _HOSTED_TERMINAL_STATUSES
        and str(state.get("content") or "").strip()
    ):
        return (
            str(state["content"]).strip(),
            str(state["status"]),
            state,
        )
    _persist_hosted_role_state(
        conversation_id,
        turn_id,
        profile=profile,
        role_stage=role_stage,
        role_label=role_label,
        state=state,
        content_fallback=start_text,
        final_report=final_report,
    )
    last_persisted_at = 0.0

    def persist() -> None:
        nonlocal last_persisted_at
        _persist_hosted_role_state(
            conversation_id,
            turn_id,
            profile=profile,
            role_stage=role_stage,
            role_label=role_label,
            state=state,
            content_fallback=start_text,
            final_report=final_report,
        )
        last_persisted_at = time.monotonic()

    def on_event(event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "thinking.delta":
            return
        current_content = str(state.get("content") or "").strip()
        previous_milestone = str(state.get("milestone_content") or "")
        if (
            event_type in {"tool.start", "subagent.start"}
            and current_content
            and current_content != previous_milestone
        ):
            milestone_count = int(state.get("milestone_count") or 0) + 1
            milestone_content = (
                current_content[len(previous_milestone):].strip()
                if previous_milestone and current_content.startswith(previous_milestone)
                else current_content
            )
            base_stage = role_stage.split(":", 1)[0]
            _persist_hosted_turn(
                conversation_id,
                turn_id,
                message={
                    "role": "assistant",
                    "name": profile,
                    "content": milestone_content,
                    "status": "completed",
                    "kind": "message",
                    "meta": {
                        "role_stage": f"{role_stage}.milestone.{milestone_count}",
                        "base_role_stage": base_stage,
                        "phase": "milestone",
                        "message_key": f"{turn_id}:{role_stage}:milestone:{milestone_count}",
                        "role_label": role_label,
                        "profile": profile,
                        "final_report": False,
                        "collapse_activities": True,
                        "activities": [dict(item) for item in state.get("activities") or []],
                        "actual_model": str(state.get("actual_model") or ""),
                        "actual_provider": str(state.get("actual_provider") or ""),
                    },
                },
            )
            state["milestone_count"] = milestone_count
            state["milestone_content"] = current_content
        had_content = bool(str(state.get("content") or ""))
        had_reasoning = any(
            activity.get("kind") == "reasoning"
            for activity in state.get("activities") or []
        )
        apply_profile_event(state, event)
        first_visible_delta = (
            event_type == "message.delta" and not had_content
        ) or (
            event_type == "reasoning.delta" and not had_reasoning
        )
        if (
            event_type not in {"message.delta", "reasoning.delta"}
            or first_visible_delta
            or time.monotonic() - last_persisted_at >= _HOSTED_EVENT_FLUSH_SECONDS
        ):
            persist()

    attempts = _HOSTED_TRANSIENT_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            result = _invoke_profile_runner(
                runner,
                runtime_profile or profile,
                prompt,
                on_event,
                kanban_task_id,
                str(state.get("runtime_session_id") or ""),
            ).strip()
            if not result:
                raise RuntimeError("Hermes profile returned an empty response")
            state["content"] = result
            state["status"] = "completed"
            state["activities"] = _remove_duplicate_reasoning_activities(
                state.get("activities") or [],
                result,
            )
            persist()
            return result, "completed", state
        except Exception as exc:
            transient = _is_transient_runtime_error(exc)
            has_tool_activity = any(
                activity.get("kind") in {"tool", "subagent"}
                for activity in state.get("activities") or []
            )
            if transient and not has_tool_activity and attempt < attempts:
                apply_profile_event(
                    state,
                    {
                        "type": "connection.retry",
                        "payload": {"message": str(exc), "attempt": attempt + 1},
                    },
                )
                state["status"] = "streaming"
                persist()
                time.sleep(0.5 * attempt)
                continue
            clean_error = sanitize_runtime_error(exc)
            partial = str(state.get("content") or "").strip()
            result = (
                f"{partial}\n\n本阶段未完成：{clean_error}"
                if partial
                else f"本阶段未完成：{clean_error}"
            )
            state["content"] = result
            state["status"] = "failed"
            state["error"] = clean_error
            state["activities"] = _remove_duplicate_reasoning_activities(
                state.get("activities") or [],
                result,
            )
            persist()
            return result, "failed", state

    raise RuntimeError("Hermes 托管角色执行状态异常")


def _run_hosted_remote_role(
    conversation_id: str,
    turn_id: str,
    *,
    profile: str,
    role_stage: str,
    role_label: str,
    prompt: str,
    kanban_task_id: str,
    start_text: str,
    artifact_required: bool = False,
    delivery_context: str = "",
    attachment_context: str = "",
    rework_round: int = 0,
) -> tuple[str, str, dict[str, Any]]:
    """Wait for a DBB3/PC connector run and project its checkpoints.

    The remote id is derived from the conversation, turn, role phase and
    profile. Restarting the dashboard therefore reattaches to the same queue
    item instead of creating another remote Kanban task.
    """

    with _STATE_LOCK:
        state = load_single_state()
        conversation = _conversation_by_id(state, conversation_id)
        hosted = (conversation.get("hosted_turns") or {}).get(turn_id)
        if not isinstance(hosted, dict):
            raise RuntimeError("托管任务记录不存在")
        title = str(hosted.get("title") or summarize_task_title(hosted.get("content")))
        run_snapshot = dict(hosted)
    remote = _ensure_remote_run(
        conversation_id,
        turn_id,
        role_stage=role_stage,
        profile=profile,
        title=title,
        objective=prompt,
        local_task_id=kanban_task_id,
        artifact_required=artifact_required,
        delivery_context=delivery_context,
        attachment_context=attachment_context,
        attachment_ids=list(run_snapshot.get("attachment_ids") or []),
        attempt=rework_round + 1,
    )
    if str(remote.get("status") or "queued") not in _REMOTE_TERMINAL_STATUSES:
        _remote_run_state_message(
            conversation_id,
            turn_id,
            remote,
            role_label=role_label,
        )

    revision = -1
    deadline = time.monotonic() + float(
        os.environ.get("HERMES_REMOTE_RUN_WAIT_SECONDS", "86400")
    )
    while True:
        with _STATE_LOCK:
            state = load_single_state()
            location = _remote_run_location(state, str(remote.get("id") or ""))
            if location is None:
                raise RuntimeError("远程执行记录不存在")
            _conversation, _hosted, _role_key, current = location
            remote = dict(current)
            cancel_requested = bool(_hosted.get("cancel_requested"))
        if cancel_requested and str(remote.get("status") or "") not in _REMOTE_TERMINAL_STATUSES:
            _persist_hosted_turn(
                conversation_id,
                turn_id,
                patch={
                    "remote_cancel_pending": True,
                    "stage": "cancel_requested",
                },
            )
            return "远程执行已请求取消。", "failed", {
                "content": "远程执行已请求取消。",
                "status": "failed",
                "activities": list(remote.get("activities") or []),
            }
        status = str(remote.get("status") or "queued")
        if status in _REMOTE_TERMINAL_STATUSES:
            result = str(remote.get("result") or remote.get("summary") or "").strip()
            if status != "completed" and remote.get("error"):
                result = result or f"远程执行失败：{remote['error']}"
            result = result or ("远程执行完成。" if status == "completed" else "远程执行未通过。")
            role_state = {
                "content": result,
                "status": "completed" if status == "completed" else "failed",
                "activities": [dict(item) for item in remote.get("activities") or [] if isinstance(item, dict)],
                "actual_model": str(remote.get("actual_model") or ""),
                "actual_provider": str(remote.get("actual_provider") or ""),
                "started_at": int(remote.get("started_at") or remote.get("created_at") or int(time.time() * 1000)),
                "milestone_count": 0,
                "milestone_content": "",
            }
            _persist_hosted_role_state(
                conversation_id,
                turn_id,
                profile=profile,
                role_stage=role_stage,
                role_label=role_label,
                state=role_state,
                content_fallback=result,
            )
            return result, role_state["status"], role_state
        _remote_run_state_message(
            conversation_id,
            turn_id,
            remote,
            role_label=role_label,
        )
        if time.monotonic() >= deadline:
            error = "远程执行器在规定时间内没有完成"
            _persist_hosted_turn(
                conversation_id,
                turn_id,
                patch={"stage": "failed", "error": error},
            )
            return f"本阶段未完成：{error}", "failed", {
                "content": error,
                "status": "failed",
                "activities": list(remote.get("activities") or []),
            }
        revision = _wait_for_hosted_update(revision, 15.0)


def create_hosted_turn_record(
    conversation: dict[str, Any],
    *,
    turn_id: str,
    content: str,
    title: str,
    profiles: list[str],
    artifact_required: bool,
    attachment_context: str = "",
    delivery_context: str = "",
    user_delivery_context: str = "",
    mode: str = "work",
    route_metadata: Optional[dict[str, Any]] = None,
    output_dir: str = "",
    attachment_ids: Optional[list[str]] = None,
) -> dict[str, Any]:
    normalized_turn_id = str(turn_id or "").strip()
    if not normalized_turn_id:
        raise ValueError("turn_id is required")
    hosted_turns = conversation.get("hosted_turns")
    if not isinstance(hosted_turns, dict):
        hosted_turns = {}
        conversation["hosted_turns"] = hosted_turns
    existing = hosted_turns.get(normalized_turn_id)
    if isinstance(existing, dict):
        return existing
    now = int(time.time() * 1000)
    normalized_mode = str(mode or "work").strip().lower()
    if normalized_mode not in {"chat", "work"}:
        normalized_mode = "work"
    route_metadata = dict(route_metadata or {})
    artifact_producers = _artifact_producer_profiles(
        route_metadata,
        profiles,
        required=bool(artifact_required),
    )
    record = {
        "turn_id": normalized_turn_id,
        "status": "queued",
        "stage": "queued",
        "content": str(content or "").strip(),
        "title": str(title or "").strip() or summarize_task_title(content),
        "profiles": list(
            dict.fromkeys(str(item).strip() for item in profiles if str(item).strip())
        ),
        "artifact_required": bool(artifact_required),
        "artifact_producer_profiles": artifact_producers,
        "artifact": dict(route_metadata.get("artifact") or {}),
        "mode": normalized_mode,
        "route_metadata": route_metadata,
        "attachment_ids": list(
            dict.fromkeys(
                str(item).strip()
                for item in (attachment_ids or [])
                if str(item).strip().startswith("file_")
            )
        ),
        "attachment_context": str(attachment_context or "").strip(),
        "delivery_context": str(delivery_context or "").strip(),
        "user_delivery_context": str(user_delivery_context or "").strip(),
        "output_dir": str(output_dir or "").strip(),
        "cancel_requested": False,
        "created_at": now,
        "updated_at": now,
    }
    hosted_turns[normalized_turn_id] = record
    conversation["updated_at"] = now
    return record


def hosted_artifact_instruction(
    run: dict[str, Any],
    *,
    remote_workers: bool,
) -> str:
    if not bool(run.get("artifact_required")):
        return "本任务未要求交付文件。不要创建、复制或上传文件，只提交文字结果和必要证据。"
    if not remote_workers:
        return str(run.get("delivery_context") or "").strip()
    user_context = str(run.get("user_delivery_context") or "").strip()
    return "\n".join(
        item
        for item in (
            user_context,
            "This role executes on a remote DBB3 worker; the public server output directory is not mounted here.",
            "Create every requested deliverable inside $HERMES_KANBAN_WORKSPACE using an absolute path.",
            "Before exiting, call kanban_complete(artifacts=[...]) with every deliverable absolute path so the cloud connector uploads it to the account file library.",
            "Do not rely on a prose path or a Kanban comment as artifact registration.",
        )
        if item
    )


def _artifact_producer_profiles(
    route_metadata: dict[str, Any],
    profiles: list[str],
    *,
    required: bool,
) -> list[str]:
    """Assign overall file delivery to specific execution lanes."""

    workers = [
        str(profile).strip()
        for profile in profiles
        if collaboration_role(str(profile)) == "worker"
    ]
    if not required or not workers:
        return []
    artifact = route_metadata.get("artifact")
    artifact = dict(artifact) if isinstance(artifact, dict) else {}
    requested = [
        str(value).strip().lower()
        for value in artifact.get("producer_profiles") or []
        if str(value).strip()
    ]
    requested.extend(
        f"{str(value).strip().lower()}-worker"
        for value in artifact.get("producer_targets") or []
        if str(value).strip().lower() in {"dbb3", "pc"}
    )
    selected = [profile for profile in workers if profile.lower() in requested]
    return list(dict.fromkeys(selected or workers[:1]))


def create_hosted_kanban_task(
    *,
    conversation_id: str,
    turn_id: str,
    title: str,
    content: str,
    profiles: Optional[list[str]] = None,
    output_dir: str = "",
) -> dict[str, Any]:
    from hermes_cli import kanban_db, kanban_decompose

    kanban_db.init_db()
    conn = kanban_db.connect()
    try:
        lane_context = ", ".join(profiles or [])
        # The execution path stays in the hosted run and role prompt. Kanban
        # bodies are user-visible and must not expose server filesystem paths.
        task_body = "\n\n".join(
            item
            for item in (
                content,
                f"Required execution lanes: {lane_context}." if lane_context else "",
            )
            if item
        )
        task_id = kanban_db.create_task(
            conn,
            title=title,
            body=task_body,
            created_by="unified-webui-hosted",
            workspace_kind="scratch",
            triage=True,
            idempotency_key=f"collaboration:{conversation_id}:{turn_id}",
            goal_mode=True,
            session_id=conversation_id,
        )
    finally:
        conn.close()
    try:
        outcome = kanban_decompose.decompose_task(
            task_id,
            author="unified-webui-hosted",
        )
        child_ids = outcome.child_ids or []
        profile_task_ids: dict[str, str] = {}
        if child_ids:
            conn = kanban_db.connect()
            try:
                for child_id in child_ids:
                    child = kanban_db.get_task(conn, child_id)
                    if child is not None and child.assignee:
                        profile_task_ids.setdefault(str(child.assignee), str(child.id))
            finally:
                conn.close()
        return {
            "task_id": task_id,
            "fanout": bool(outcome.fanout),
            "child_ids": child_ids,
            "profile_task_ids": profile_task_ids,
            "reason": outcome.reason,
        }
    except Exception as exc:
        return {
            "task_id": task_id,
            "fanout": False,
            "child_ids": [],
            "reason": f"任务拆分暂时失败：{exc}",
        }


def _notify_hosted_update() -> int:
    global _HOSTED_UPDATE_REVISION
    with _HOSTED_UPDATE_CONDITION:
        _HOSTED_UPDATE_REVISION += 1
        _HOSTED_UPDATE_CONDITION.notify_all()
        return _HOSTED_UPDATE_REVISION


def _completion_notification_record(
    conversation_id: str,
    turn_id: str,
    status: str,
    result: str,
) -> dict[str, Any]:
    now = int(time.time() * 1000)
    collapse_id = "hermes-turn-" + hashlib.sha256(
        f"{conversation_id}\0{turn_id}".encode("utf-8")
    ).hexdigest()[:40]
    return {
        "id": collapse_id,
        "state": "queued",
        "task_status": str(status or "completed").strip().lower(),
        "result": str(result or "").strip()[:50_000],
        "collapse_id": collapse_id,
        "attempts": 0,
        "deliveries": {},
        "last_error": "",
        "next_attempt_at": now,
        "created_at": now,
        "updated_at": now,
    }


def _schedule_mobile_completion_notification(
    conversation_id: str,
    turn_id: str,
    status: str,
    result: str,
) -> None:
    """Queue a persisted notification on the process-wide APNs dispatcher."""

    global _MOBILE_NOTIFICATION_DISPATCH_THREAD

    try:
        with _STATE_LOCK:
            state = load_single_state()
            conversation = _conversation_by_id(state, conversation_id)
            run = (conversation.get("hosted_turns") or {}).get(turn_id)
            if not isinstance(run, dict):
                return
            owner_id = str(conversation.get("owner_id") or "").strip()
            notification = run.get("notification")
            if not isinstance(notification, dict):
                notification = _completion_notification_record(
                    conversation_id,
                    turn_id,
                    status,
                    result,
                )
                run["notification"] = notification
                save_single_state(state)
            notification_state = str(notification.get("state") or "queued")
        if notification_state in _MOBILE_NOTIFICATION_TERMINAL_STATUSES:
            return
        if not owner_id:
            _persist_notification_outcome(
                conversation_id,
                turn_id,
                {
                    "state": "no_recipients",
                    "deliveries": {},
                    "error": "notification_owner_missing",
                },
            )
            return
        key = f"{conversation_id}:{turn_id}"
        due_at = max(
            int(time.time() * 1000),
            int(notification.get("next_attempt_at") or 0),
        )
        with _MOBILE_NOTIFICATION_DISPATCH_CONDITION:
            current = _MOBILE_NOTIFICATION_PENDING.get(key)
            if current is None or due_at < current[2]:
                _MOBILE_NOTIFICATION_PENDING[key] = (
                    conversation_id,
                    turn_id,
                    due_at,
                )
            if (
                _MOBILE_NOTIFICATION_DISPATCH_THREAD is None
                or not _MOBILE_NOTIFICATION_DISPATCH_THREAD.is_alive()
            ):
                _MOBILE_NOTIFICATION_DISPATCH_THREAD = threading.Thread(
                    target=_mobile_notification_dispatch_loop,
                    name="hermes-apns-dispatcher",
                    # Claims and per-device outcomes are durable before each
                    # network step, so process exit leaves replayable work.
                    daemon=True,
                )
                _MOBILE_NOTIFICATION_DISPATCH_THREAD.start()
            _MOBILE_NOTIFICATION_DISPATCH_CONDITION.notify()
    except Exception:
        # The persisted outbox remains available for startup replay.
        return


def _mobile_notification_dispatch_loop() -> None:
    """Deliver all persistent APNs jobs without allocating one thread per job."""

    while True:
        with _MOBILE_NOTIFICATION_DISPATCH_CONDITION:
            while not _MOBILE_NOTIFICATION_PENDING:
                _MOBILE_NOTIFICATION_DISPATCH_CONDITION.wait()
            key, pending = min(
                _MOBILE_NOTIFICATION_PENDING.items(),
                key=lambda item: item[1][2],
            )
            conversation_id, turn_id, due_at = pending
            remaining_ms = due_at - int(time.time() * 1000)
            if remaining_ms > 0:
                _MOBILE_NOTIFICATION_DISPATCH_CONDITION.wait(
                    timeout=remaining_ms / 1000
                )
                continue
            _MOBILE_NOTIFICATION_PENDING.pop(key, None)

        try:
            retry_delay_ms = _deliver_persisted_completion_notification(
                conversation_id,
                turn_id,
            )
        except Exception:
            retry_delay_ms = 60_000
        if retry_delay_ms is not None:
            with _MOBILE_NOTIFICATION_DISPATCH_CONDITION:
                _MOBILE_NOTIFICATION_PENDING[key] = (
                    conversation_id,
                    turn_id,
                    int(time.time() * 1000) + max(0, retry_delay_ms),
                )
                _MOBILE_NOTIFICATION_DISPATCH_CONDITION.notify()


def _deliver_persisted_completion_notification(
    conversation_id: str,
    turn_id: str,
) -> Optional[int]:
    with _STATE_LOCK:
        state = load_single_state()
        conversation = _conversation_by_id(state, conversation_id)
        run = (conversation.get("hosted_turns") or {}).get(turn_id)
        if not isinstance(run, dict):
            return None
        notification = run.get("notification")
        if not isinstance(notification, dict):
            return None
        notification_state = str(notification.get("state") or "queued")
        if notification_state in _MOBILE_NOTIFICATION_TERMINAL_STATUSES:
            return None
        now = int(time.time() * 1000)
        next_attempt_at = int(notification.get("next_attempt_at") or 0)
        if notification_state == "retry" and next_attempt_at > now:
            return next_attempt_at - now
        owner_id = str(conversation.get("owner_id") or "").strip()
        claimed = {
            **notification,
            "state": "delivering",
            "attempts": int(notification.get("attempts") or 0) + 1,
            "updated_at": now,
        }
        run["notification"] = claimed
        save_single_state(state)

    if not owner_id:
        return _persist_notification_outcome(
            conversation_id,
            turn_id,
            {
                "state": "no_recipients",
                "deliveries": dict(claimed.get("deliveries") or {}),
                "error": "notification_owner_missing",
            },
        )

    def persist_progress(deliveries: dict[str, dict[str, Any]]) -> None:
        with _STATE_LOCK:
            current_state = load_single_state()
            current_conversation = _conversation_by_id(current_state, conversation_id)
            current_run = (current_conversation.get("hosted_turns") or {}).get(turn_id)
            if not isinstance(current_run, dict):
                return
            current = current_run.get("notification")
            if not isinstance(current, dict):
                return
            current_run["notification"] = {
                **current,
                "deliveries": deliveries,
                "updated_at": int(time.time() * 1000),
            }
            save_single_state(current_state)

    try:
        from hermes_cli.dashboard_auth.mobile_notifications import (
            deliver_task_completion_push,
        )

        outcome = deliver_task_completion_push(
            owner_id=owner_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            status=str(claimed.get("task_status") or "completed"),
            result=str(claimed.get("result") or ""),
            collapse_id=str(claimed.get("collapse_id") or claimed.get("id") or ""),
            previous_deliveries=dict(claimed.get("deliveries") or {}),
            progress_callback=persist_progress,
        )
    except Exception as exc:
        outcome = {
            "state": "retry",
            "deliveries": dict(claimed.get("deliveries") or {}),
            "error": f"notification_delivery_failed:{type(exc).__name__}"[:240],
        }
    return _persist_notification_outcome(conversation_id, turn_id, outcome)


def _persist_notification_outcome(
    conversation_id: str,
    turn_id: str,
    outcome: dict[str, Any],
) -> Optional[int]:
    with _STATE_LOCK:
        state = load_single_state()
        conversation = _conversation_by_id(state, conversation_id)
        run = (conversation.get("hosted_turns") or {}).get(turn_id)
        if not isinstance(run, dict):
            return None
        notification = run.get("notification")
        if not isinstance(notification, dict):
            return None
        now = int(time.time() * 1000)
        outcome_state = str(outcome.get("state") or "retry")
        error = str(outcome.get("error") or "")[:240]
        next_attempt_at = 0
        retry_delay_ms: Optional[int] = None
        if outcome_state == "retry":
            retry_delay_ms = _notification_retry_delay_ms(
                int(notification.get("attempts") or 1)
            )
            if error == "apns_not_configured":
                retry_delay_ms = 5 * 60 * 1000
            next_attempt_at = now + retry_delay_ms
        run["notification"] = {
            **notification,
            "state": outcome_state,
            "deliveries": dict(outcome.get("deliveries") or {}),
            "last_error": error,
            "next_attempt_at": next_attempt_at,
            "updated_at": now,
            **(
                {"completed_at": now}
                if outcome_state in _MOBILE_NOTIFICATION_TERMINAL_STATUSES
                else {}
            ),
        }
        save_single_state(state)
    _notify_hosted_update()
    if outcome_state != "retry":
        return None
    return retry_delay_ms


def _notification_retry_delay_ms(attempts: int) -> int:
    exponent = max(0, min(int(attempts) - 1, 8))
    return min(60 * 60 * 1000, 15_000 * (2**exponent))


def _wait_for_hosted_update(revision: int, timeout: float = 15.0) -> int:
    with _HOSTED_UPDATE_CONDITION:
        if _HOSTED_UPDATE_REVISION <= revision:
            _HOSTED_UPDATE_CONDITION.wait(timeout=timeout)
        return _HOSTED_UPDATE_REVISION


def _persist_hosted_turn(
    conversation_id: str,
    turn_id: str,
    *,
    patch: Optional[dict[str, Any]] = None,
    message: Optional[dict[str, Any]] = None,
    runtime_session: Optional[tuple[str, str]] = None,
) -> dict[str, Any]:
    with _STATE_LOCK:
        state = load_single_state()
        conversation = _conversation_by_id(state, conversation_id)
        run = (conversation.get("hosted_turns") or {}).get(turn_id)
        if not isinstance(run, dict):
            raise RuntimeError("托管任务记录不存在")
        now = int(time.time() * 1000)
        if patch:
            for key, value in patch.items():
                if key == "role_events" and isinstance(value, dict):
                    role_events = run.get("role_events")
                    if not isinstance(role_events, dict):
                        role_events = {}
                        run["role_events"] = role_events
                    role_events.update(value)
                else:
                    run[key] = value
        run["updated_at"] = now
        conversation["updated_at"] = now
        if runtime_session:
            runtime_profile, runtime_session_id = runtime_session
            if str(runtime_session_id or "").strip():
                set_conversation_runtime_session(
                    conversation,
                    str(runtime_profile or "default"),
                    str(runtime_session_id),
                )
        if message:
            message_meta = dict(message.get("meta") or {})
            message_meta.setdefault("runtime_turn_id", turn_id)
            role_stage = str(message_meta.get("role_stage") or "")
            default_phase = str(
                message_meta.get("phase")
                or message.get("status")
                or "completed"
            )
            message_meta.setdefault("phase", default_phase)
            if role_stage:
                message_meta.setdefault(
                    "message_key",
                    f"{turn_id}:{role_stage}:{default_phase}",
                )
            message_key = str(message_meta.get("message_key") or "")

            def matches_message(item: dict[str, Any]) -> bool:
                item_meta = item.get("meta")
                if not isinstance(item_meta, dict):
                    return False
                if item_meta.get("runtime_turn_id") != turn_id:
                    return False
                if message_key:
                    return str(item_meta.get("message_key") or "") == message_key
                return (
                    str(item_meta.get("role_stage") or "") == role_stage
                    and str(item.get("kind") or "message")
                    == str(message.get("kind") or "message")
                )

            existing = next(
                (
                    item
                    for item in conversation.get("messages") or []
                    if matches_message(item)
                ),
                None,
            )
            if existing is None:
                existing = _append_message(
                    conversation,
                    role=str(message.get("role") or "assistant"),
                    name=str(message.get("name") or "default"),
                    content=str(message.get("content") or "").strip(),
                    status=str(message.get("status") or "completed"),
                    kind=str(message.get("kind") or "message"),
                    meta=message_meta,
                )
            else:
                existing.update(
                    {
                        "content": str(message.get("content") or "").strip(),
                        "status": str(message.get("status") or "completed"),
                        "meta": {**existing.get("meta", {}), **message_meta},
                        "updated_at": now,
                    }
                )
                _project_native_message(existing)
        save_single_state(state)
        persisted = dict(run)
    _notify_hosted_update()
    return persisted


def _remote_run_location(
    state: dict[str, Any],
    remote_run_id: str,
) -> tuple[dict[str, Any], dict[str, Any], str, dict[str, Any]] | None:
    """Return conversation, hosted turn, role key and remote run by id."""

    wanted = str(remote_run_id or "").strip()
    if not wanted:
        return None
    for conversation in state.get("conversations") or []:
        if not isinstance(conversation, dict):
            continue
        for turn in (conversation.get("hosted_turns") or {}).values():
            if not isinstance(turn, dict):
                continue
            for role_key, remote_run in (turn.get("remote_runs") or {}).items():
                if isinstance(remote_run, dict) and str(remote_run.get("id") or "") == wanted:
                    return conversation, turn, str(role_key), remote_run
    return None


def _remote_run_public(remote_run: dict[str, Any]) -> dict[str, Any]:
    """Return the connector-safe subset of a remote run record."""

    return {
        key: remote_run.get(key)
        for key in (
            "id",
            "idempotency_key",
            "connector_id",
            "profile",
            "title",
            "objective",
            "local_task_id",
            "attempt",
            "max_runtime_seconds",
            "artifact_required",
            "created_at",
            "updated_at",
            "status",
            "checkpoint_cursor",
            "remote_task_id",
            "root_task_id",
            "session_id",
            "cancel_requested",
            "cancel_reason",
            "delivery_context",
            "attachment_context",
            "attachment_ids",
        )
        if key in remote_run
    }


def _connector_for_profile(profile: str) -> str:
    normalized = str(profile or "").strip().lower()
    if normalized == "pc-worker":
        return "pc-primary"
    return "dbb3-primary"


def _remote_run_connector_id(remote_run: dict[str, Any]) -> str:
    return str(
        remote_run.get("connector_id")
        or _connector_for_profile(str(remote_run.get("profile") or ""))
    ).strip()[:128]


def _remote_run_id(
    conversation_id: str,
    turn_id: str,
    role_stage: str,
    profile: str,
) -> str:
    digest = hashlib.sha256(
        f"{conversation_id}:{turn_id}:{role_stage}:{profile}".encode("utf-8")
    ).hexdigest()[:24]
    return f"remote_{digest}"


def _ensure_remote_run(
    conversation_id: str,
    turn_id: str,
    *,
    role_stage: str,
    profile: str,
    title: str,
    objective: str,
    local_task_id: str,
    artifact_required: bool,
    delivery_context: str,
    attachment_context: str,
    attachment_ids: Optional[list[str]] = None,
    attempt: int = 1,
) -> dict[str, Any]:
    """Create or reuse the durable DBB3/PC queue item for one role phase."""

    remote_id = _remote_run_id(conversation_id, turn_id, role_stage, profile)
    with _STATE_LOCK:
        state = load_single_state()
        conversation = _conversation_by_id(state, conversation_id)
        hosted = (conversation.get("hosted_turns") or {}).get(turn_id)
        if not isinstance(hosted, dict):
            raise RuntimeError("托管任务记录不存在")
        remote_runs = hosted.get("remote_runs")
        if not isinstance(remote_runs, dict):
            remote_runs = {}
            hosted["remote_runs"] = remote_runs
        existing = remote_runs.get(role_stage)
        if isinstance(existing, dict) and str(existing.get("id") or "") == remote_id:
            return dict(existing)
        now = int(time.time() * 1000)
        record = {
            "id": remote_id,
            "idempotency_key": f"collaboration:{conversation_id}:{turn_id}:{role_stage}",
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "role_stage": role_stage,
            "profile": profile,
            "connector_id": _connector_for_profile(profile),
            "title": title,
            "objective": objective,
            "local_task_id": local_task_id,
            "attempt": max(1, int(attempt)),
            "max_runtime_seconds": 1800 if profile == "pc-worker" else 900,
            "artifact_required": bool(artifact_required),
            "delivery_context": delivery_context,
            "attachment_context": attachment_context,
            "attachment_ids": list(
                dict.fromkeys(
                    str(item).strip()
                    for item in (attachment_ids or [])
                    if str(item).strip().startswith("file_")
                )
            ),
            "status": "queued",
            "checkpoint_cursor": 0,
            "activities": [],
            "result": "",
            "summary": "",
            "error": "",
            "cancel_requested": False,
            "cancel_reason": "",
            "created_at": now,
            "updated_at": now,
        }
        remote_runs[role_stage] = record
        save_single_state(state)
    _notify_hosted_update()
    return dict(record)


def _remote_run_state_message(
    conversation_id: str,
    turn_id: str,
    remote_run: dict[str, Any],
    *,
    role_label: str,
) -> None:
    """Project a remote checkpoint into the same collapsible native message."""

    profile = str(remote_run.get("profile") or "default")
    role_stage = str(remote_run.get("role_stage") or f"worker:{profile}")
    status = str(remote_run.get("status") or "queued")
    semantic_progress = str(
        remote_run.get("summary") or remote_run.get("result") or ""
    ).strip()
    result = str(remote_run.get("result") or remote_run.get("summary") or "").strip()
    if status in _REMOTE_TERMINAL_STATUSES:
        content = result or (
            f"远程执行已结束：{remote_run.get('error')}"
            if remote_run.get("error")
            else "远程执行已结束。"
        )
    elif status == "running":
        content = result or "已连接远程执行器，正在执行。"
    else:
        content = "已排队等待远程执行器领取。"
    state = {
        "content": content,
        "status": "completed" if status == "completed" else "failed" if status in {"failed", "cancelled"} else "streaming",
        "activities": [dict(item) for item in remote_run.get("activities") or [] if isinstance(item, dict)],
        "actual_model": str(remote_run.get("actual_model") or ""),
        "actual_provider": str(remote_run.get("actual_provider") or ""),
        "started_at": int(remote_run.get("started_at") or remote_run.get("created_at") or int(time.time() * 1000)),
        "completed_at": int(remote_run.get("completed_at") or 0) or None,
        "updated_at": int(remote_run.get("updated_at") or int(time.time() * 1000)),
    }
    _persist_hosted_role_state(
        conversation_id,
        turn_id,
        profile=profile,
        role_stage=role_stage,
        role_label=role_label,
        state=state,
        content_fallback=content,
        semantic_milestone=semantic_progress if status == "running" else "",
    )


def request_hosted_turn_cancellation(
    conversation_id: str,
    turn_id: str,
    *,
    reason: str = "用户取消",
) -> dict[str, Any]:
    with _STATE_LOCK:
        state = load_single_state()
        conversation = _conversation_by_id(state, conversation_id)
        run = (conversation.get("hosted_turns") or {}).get(turn_id)
        if not isinstance(run, dict):
            raise RuntimeError("托管任务记录不存在")
        if run.get("status") in _HOSTED_TERMINAL_STATUSES:
            return dict(run)
        now = int(time.time() * 1000)
        run.update(
            {
                "cancel_requested": True,
                "cancel_reason": str(reason or "用户取消").strip() or "用户取消",
                "cancel_requested_at": now,
                "updated_at": now,
            }
        )
        conversation["updated_at"] = now
        save_single_state(state)
        persisted = dict(run)
    _notify_hosted_update()
    return persisted


def _finish_hosted_turn_if_cancelled(
    conversation_id: str,
    turn_id: str,
) -> bool:
    with _STATE_LOCK:
        state = load_single_state()
        conversation = _conversation_by_id(state, conversation_id)
        run = (conversation.get("hosted_turns") or {}).get(turn_id)
        if not isinstance(run, dict) or not run.get("cancel_requested"):
            return False
        if run.get("status") in {"completed", "cancelled"}:
            return run.get("status") == "cancelled"
        reason = str(run.get("cancel_reason") or "用户取消")
    now = int(time.time() * 1000)
    _persist_hosted_turn(
        conversation_id,
        turn_id,
        patch={
            "status": "cancelled",
            "stage": "cancelled",
            "cancelled_at": now,
            "completed_at": now,
        },
        message={
            "role": "assistant",
            "name": "default",
            "content": f"任务已取消：{reason}",
            "status": "cancelled",
            "kind": "message",
            "meta": {
                "role_stage": "reporter",
                "role_label": "Hermes · 任务取消",
                "final_report": True,
            },
        },
    )
    return True


def _review_requests_rework(result: str) -> bool:
    """Resolve the review gate from its explicit marker with a text fallback."""

    text = str(result or "").strip()
    marker = re.search(
        r"HERMES_REVIEW\s*:\s*(PASS|REWORK)",
        text,
        flags=re.IGNORECASE,
    )
    if marker:
        return marker.group(1).upper() == "REWORK"
    normalized = text.lower()
    return any(
        phrase in normalized
        for phrase in (
            "需要返工",
            "退回执行",
            "退回修改",
            "审阅未通过",
            "验收未通过",
            "changes requested",
            "request rework",
            "review failed",
        )
    )


def _hosted_chat_attachment_context(
    conversation: dict[str, Any],
    run: dict[str, Any],
) -> str:
    """Resolve chat inputs from the account file library, not client paths."""

    attachment_ids = [
        str(item).strip()
        for item in run.get("attachment_ids") or []
        if str(item).strip().startswith("file_")
    ]
    if not attachment_ids:
        return str(run.get("attachment_context") or "").strip()
    owner_id = str(conversation.get("owner_id") or LOCAL_OWNER_ID).strip()
    lines: list[str] = []
    library = _file_library()
    for file_id in attachment_ids:
        try:
            record, stored_path = library.resolve_download(owner_id, file_id)
        except (KeyError, FileNotFoundError, ValueError, OSError):
            continue
        path = str(stored_path)
        name = re.sub(r"[\r\n]+", " ", str(record.get("name") or file_id)).strip()
        mime_type = re.sub(
            r"[\r\n]+",
            " ",
            str(record.get("mime_type") or "application/octet-stream"),
        ).strip()
        lines.append(
            f"- {name}: {path} (id={file_id}, type={mime_type}, size={int(record.get('size') or 0)} bytes)"
        )
    if lines:
        return "本轮账户云端持久附件：\n" + "\n".join(lines)
    return "本轮附件记录当前不可用：" + "、".join(attachment_ids)


def execute_hosted_chat(
    conversation_id: str,
    turn_id: str,
    *,
    runner: Callable[[str, str], str] = run_profile_turn,
) -> None:
    """Run a durable simple turn through exactly one default Hermes."""
    with _STATE_LOCK:
        state = load_single_state()
        conversation = _conversation_by_id(state, conversation_id)
        run = (conversation.get("hosted_turns") or {}).get(turn_id)
        if not isinstance(run, dict):
            raise RuntimeError("Hosted turn record does not exist")
        if run.get("status") in _HOSTED_TERMINAL_STATUSES:
            return
        now = int(time.time() * 1000)
        run.update({"status": "running", "stage": "chat", "updated_at": now})
        run.setdefault("started_at", now)
        _ensure_hosted_output_baseline(conversation_id, run)
        save_single_state(state)
        conversation_snapshot = dict(conversation)
        run = dict(run)
    if _finish_hosted_turn_if_cancelled(conversation_id, turn_id):
        return
    content = str(run.get("content") or "").strip()
    selected_profiles = [
        str(item).strip() for item in run.get("profiles") or [] if str(item).strip()
    ]
    profile = selected_profiles[0] if selected_profiles else str(
        conversation_snapshot.get("profile") or "default"
    )
    attachment_context = _hosted_chat_attachment_context(
        conversation_snapshot,
        run,
    )
    delivery_context = str(run.get("delivery_context") or "").strip()
    content = "\n\n".join(
        item
        for item in (
            content,
            (
                f"{attachment_context}\n"
                "这些附件是本轮输入；请先读取实际文件，再根据用户要求回答。"
                if attachment_context
                else ""
            ),
            delivery_context,
        )
        if item
    )
    runtime_session_id = str(
        (conversation_snapshot.get("runtime_sessions") or {}).get(profile) or ""
    ).strip()
    result, status, _role_state = _run_hosted_role(
        conversation_id,
        turn_id,
        profile=profile,
        role_stage="chat",
        role_label="Hermes",
        prompt=build_single_prompt(
            conversation_snapshot,
            profile,
            content,
            include_projected_history=not bool(runtime_session_id),
        ),
        runner=runner,
        kanban_task_id="",
        runtime_session_id=runtime_session_id,
        start_text="收到消息，正在处理。",
        previous_state=(run.get("role_events") or {}).get("chat"),
    )
    if _finish_hosted_turn_if_cancelled(conversation_id, turn_id):
        return
    final_status = "completed" if status == "completed" else "failed"
    published_attachments: list[dict[str, Any]] = []
    if bool(run.get("artifact_required")) and final_status == "completed":
        published_attachments = _hosted_turn_output_attachments(
            conversation_id,
            turn_id,
            int(run.get("started_at") or 0),
        )
        if not published_attachments:
            final_status = "failed"
            status = "failed"
            result = (
                f"{result}\n\n文件交付失败：Hermes 未在指定输出目录生成可下载文件。"
            ).strip()
    now = int(time.time() * 1000)
    _persist_hosted_turn(
        conversation_id,
        turn_id,
        patch={
            "chat_result": result,
            "chat_status": status,
            "status": final_status,
            "stage": final_status,
            "completed_at": now,
            "notification": _completion_notification_record(
                conversation_id,
                turn_id,
                final_status,
                result,
            ),
        },
        runtime_session=(
            profile,
            str(_role_state.get("runtime_session_id") or "").strip(),
        ),
        message={
            "role": "assistant",
            "name": profile,
            "content": result,
            "status": final_status,
            "kind": "message",
            "meta": {
                "role_stage": "chat",
                "base_role_stage": "chat",
                "phase": "completed",
                "message_key": f"{turn_id}:chat:completed",
                "role_label": "Hermes",
                "profile": profile,
                "attachments": published_attachments,
                "runtime_session_id": str(
                    _role_state.get("runtime_session_id") or ""
                ).strip(),
            },
        },
    )
    _schedule_mobile_completion_notification(
        conversation_id,
        turn_id,
        final_status,
        result,
    )


def execute_hosted_workflow(
    conversation_id: str,
    turn_id: str,
    *,
    runner: Callable[[str, str], str] = run_profile_turn,
    task_creator: Callable[..., dict[str, Any]] = create_hosted_kanban_task,
) -> None:
    with _STATE_LOCK:
        state = load_single_state()
        conversation = _conversation_by_id(state, conversation_id)
        run = (conversation.get("hosted_turns") or {}).get(turn_id)
        if not isinstance(run, dict):
            raise RuntimeError("托管任务记录不存在")
        if run.get("status") in _HOSTED_TERMINAL_STATUSES:
            return
        run["status"] = "running"
        run["stage"] = str(run.get("stage") or "preparing")
        run.setdefault("started_at", int(time.time() * 1000))
        run["updated_at"] = int(time.time() * 1000)
        _ensure_hosted_output_baseline(conversation_id, run)
        save_single_state(state)
        conversation_snapshot = dict(conversation)
        run = dict(run)

    if str(run.get("mode") or "work").lower() == "chat":
        execute_hosted_chat(conversation_id, turn_id, runner=runner)
        return

    # The public instance intentionally ships only the default Profile. Real
    # DBB3/PC work enters the connector queue; injected runners in tests keep
    # exercising the in-process contract.
    remote_workers = runner is run_profile_turn

    if _finish_hosted_turn_if_cancelled(conversation_id, turn_id):
        return

    content = str(run.get("content") or "").strip()
    title = str(run.get("title") or summarize_task_title(content))
    profiles = list(run.get("profiles") or ["default", "dbb3-worker", "reviewer"])
    ordered = collaboration_execution_order(profiles)
    worker_profiles = [
        item for item in ordered if collaboration_role(item) == "worker"
    ] or ["dbb3-worker"]
    reviewer_profile = next(
        (item for item in ordered if collaboration_role(item) == "reviewer"),
        "reviewer",
    )
    reporter_profile = next(
        (item for item in ordered if collaboration_role(item) == "reporter"),
        "default",
    )
    artifact_required = bool(run.get("artifact_required"))
    artifact_producer_profiles = set(
        str(profile)
        for profile in (
            run.get("artifact_producer_profiles")
            or _artifact_producer_profiles(
                dict(run.get("route_metadata") or {}),
                worker_profiles,
                required=artifact_required,
            )
        )
        if str(profile)
    )
    artifact_instruction = hosted_artifact_instruction(
        run,
        remote_workers=remote_workers,
    )
    attachment_context = _hosted_chat_attachment_context(conversation_snapshot, run)
    worker_kanban_instruction = (
        "官方 Kanban 是 DBB3 的唯一控制面。你可以读取根任务和已分配工作项，"
        "也可以向已分配工作项写入进度、证据和交接评论；"
        "不得创建、改派、关闭或删除根任务，也不得替 Manager 改变任务生命周期。"
    )
    reviewer_kanban_instruction = (
        "官方 Kanban 是 DBB3 的唯一控制面。你可以读取任务、证据和评论，"
        "并向已分配的审阅工作项写入验收结论；"
        "不得创建、改派、关闭或删除根任务。"
    )
    reporter_kanban_instruction = (
        "官方 Kanban 是 DBB3 的唯一控制面。你可以读取任务链用于最终汇总；"
        "不得创建、改派、关闭或删除根任务，也不要替执行者重复工作。"
    )

    task_id = str(run.get("task_id") or "")
    child_ids = [str(item) for item in run.get("child_ids") or [] if item]
    profile_task_ids = {
        str(profile): str(task)
        for profile, task in (run.get("profile_task_ids") or {}).items()
        if str(profile) and str(task)
    }
    if not task_id:
        _persist_hosted_turn(
            conversation_id,
            turn_id,
            patch={"stage": "decomposing"},
            message={
                "role": "assistant",
                "name": "dispatcher",
                "content": "收到任务，正在结合上下文拆分并安排执行。",
                "status": "streaming",
                "kind": "message",
                "meta": {
                    "role_stage": "dispatch.opening",
                    "base_role_stage": "dispatch",
                    "phase": "opening",
                    "message_key": f"{turn_id}:dispatch:opening",
                    "role_label": "Hermes · 调度",
                    "profile": "dispatcher",
                    "handoff_to": worker_profiles,
                    "final_report": False,
                },
            },
        )
        task_info = task_creator(
            conversation_id=conversation_id,
            turn_id=turn_id,
            title=title,
            content=content,
            profiles=[*worker_profiles, reviewer_profile],
            output_dir=str(run.get("output_dir") or ""),
        )
        task_id = str(task_info.get("task_id") or "")
        child_ids = [str(item) for item in task_info.get("child_ids") or [] if item]
        profile_task_ids = {
            str(profile): str(task)
            for profile, task in (task_info.get("profile_task_ids") or {}).items()
            if str(profile) and str(task)
        }
        workflow_text = (
            f"DBB3 已创建根任务并拆分为 {len(child_ids)} 个执行步骤。"
            if task_info.get("fanout")
            else "DBB3 已创建根任务，正在按能力编排执行。"
        )
        _persist_hosted_turn(
            conversation_id,
            turn_id,
            patch={
                "task_id": task_id,
                "child_ids": child_ids,
                "profile_task_ids": profile_task_ids,
                "stage": "dispatching",
            },
            message={
                "role": "system",
                "name": "工作流已启动",
                "content": workflow_text,
                "status": "completed",
                "kind": "workflow",
                "meta": {
                    "role_stage": "workflow",
                    "task_id": task_id,
                    "child_ids": child_ids,
                },
            },
        )
        if _finish_hosted_turn_if_cancelled(conversation_id, turn_id):
            return

    _persist_hosted_turn(
        conversation_id,
        turn_id,
        patch={"stage": "worker"},
        message={
            "role": "assistant",
            "name": reporter_profile,
            "content": (
                f"任务已由 DBB3 托管并派发给 {', '.join(worker_profiles)}。"
                f"完成后由 {reviewer_profile} 验收，再由我统一汇报。"
            ),
            "status": "completed",
            "kind": "message",
            "meta": {
                "role_stage": "dispatch",
                "role_label": "Hermes · 调度",
                "profile": "dispatcher",
                "handoff_to": worker_profiles,
                "final_report": False,
            },
        },
    )
    if _finish_hosted_turn_if_cancelled(conversation_id, turn_id):
        return

    if profile_task_ids:
        worker_task_scopes = {
            profile: profile_task_ids.get(profile, "")
            for profile in worker_profiles
        }
        reviewer_task_scope = profile_task_ids.get(reviewer_profile, "")
        reporter_task_scope = ""
    else:
        # Compatibility for pre-mapping persisted turns and injected task
        # creators in tests. New production turns always persist assignments.
        worker_task_scopes = {
            profile: (
                child_ids[index]
                if index < len(child_ids)
                else f"hosted-worker-{profile}-{turn_id}"
            )
            for index, profile in enumerate(worker_profiles)
        }
        reviewer_task_scope = (
            child_ids[len(worker_profiles)]
            if len(child_ids) > len(worker_profiles)
            else f"hosted-reviewer-{turn_id}"
        )
        reporter_task_scope = f"hosted-reporter-{turn_id}"

    worker_results = {
        str(profile): str(result)
        for profile, result in (run.get("worker_results") or {}).items()
        if str(profile) and str(result)
    }
    worker_statuses = {
        str(profile): str(status)
        for profile, status in (run.get("worker_statuses") or {}).items()
        if str(profile) and str(status)
    }

    def execute_worker(
        profile: str,
        *,
        rework_feedback: str = "",
        rework_round: int = 0,
    ) -> tuple[str, str, str, dict[str, Any]]:
        lane_artifact_required = (
            artifact_required and profile in artifact_producer_profiles
        )
        lane_artifact_instruction = (
            artifact_instruction
            if lane_artifact_required
            else (
                "This execution lane is not the designated file producer. "
                "Return evidence and results to the reviewer, and do not fail "
                "only because this lane creates no deliverable file."
            )
        )
        role_stage = (
            f"worker:{profile}:rework:{rework_round}"
            if rework_round
            else "worker" if len(worker_profiles) == 1 else f"worker:{profile}"
        )
        worker_prompt = "\n".join(
            item
            for item in (
                "你正在 DBB3 唯一控制面的服务端托管工作流中。",
                f"你的 Profile：{profile}",
                f"官方 Kanban 根任务：{task_id}"
                if task_id and not remote_workers
                else "",
                worker_kanban_instruction,
                "你是任务执行者。只完成调度分配给当前 Profile 和目标设备的子任务。",
                "负责实际执行、工具调用、证据收集和必要产物创建。",
                "可以使用所有已配置的 Skill、MCP 和工具；正常的搜索、命令、取证和验证属于执行过程。",
                "工作过程中按真实进展自然汇报，不套固定中间模板。",
                "不要做最终总结；把结果、证据、耗时和遗留问题提交给审阅者。",
                f"用户任务：{content}",
                (
                    f"审阅者退回意见（第 {rework_round} 轮返工）：\n{rework_feedback}"
                    if rework_feedback
                    else ""
                ),
                attachment_context,
                lane_artifact_instruction,
            )
            if item
        )
        if remote_workers:
            result, status, role_state = _run_hosted_remote_role(
                conversation_id,
                turn_id,
                profile=profile,
                role_stage=role_stage,
                role_label=f"{profile} · 执行",
                prompt=worker_prompt,
                kanban_task_id=worker_task_scopes[profile],
                start_text="收到分配的子任务，正在执行。",
                artifact_required=lane_artifact_required,
                delivery_context=lane_artifact_instruction,
                attachment_context=attachment_context,
                rework_round=rework_round,
            )
        else:
            result, status, role_state = _run_hosted_role(
                conversation_id,
                turn_id,
                profile=profile,
                role_stage=role_stage,
                role_label=f"{profile} · 执行",
                prompt=worker_prompt,
                runner=runner,
                kanban_task_id=worker_task_scopes[profile],
                start_text="收到分配的子任务，正在执行。",
                previous_state=(run.get("role_events") or {}).get(role_stage),
            )
        return profile, result, status, role_state

    pending_workers = [
        profile
        for profile in worker_profiles
        if worker_statuses.get(profile) != "completed" or not worker_results.get(profile)
    ]
    if pending_workers:
        with ThreadPoolExecutor(
            max_workers=len(pending_workers),
            thread_name_prefix=f"hosted-workers-{turn_id[-8:]}",
        ) as executor:
            futures = {
                executor.submit(execute_worker, profile): profile
                for profile in pending_workers
            }
            for future in as_completed(futures):
                profile, result, status, _worker_state = future.result()
                worker_results[profile] = result
                worker_statuses[profile] = status
                _persist_hosted_turn(
                    conversation_id,
                    turn_id,
                    patch={
                        "worker_results": dict(worker_results),
                        "worker_statuses": dict(worker_statuses),
                        "stage": "worker",
                    },
                )
    worker_result = "\n\n".join(
        f"## {profile}\n{worker_results.get(profile, '')}".strip()
        for profile in worker_profiles
    )
    worker_status = (
        "completed"
        if all(worker_statuses.get(profile) == "completed" for profile in worker_profiles)
        else "failed"
    )
    _persist_hosted_turn(
        conversation_id,
        turn_id,
        patch={
            "worker_result": worker_result,
            "worker_status": worker_status,
            "worker_results": dict(worker_results),
            "worker_statuses": dict(worker_statuses),
            "stage": "reviewer",
        },
    )
    if _finish_hosted_turn_if_cancelled(conversation_id, turn_id):
        return

    reviewer_result = str(run.get("reviewer_result") or "")
    reviewer_status = str(run.get("reviewer_status") or "")
    if not reviewer_result:
        reviewer_prompt = "\n".join(
            item
            for item in (
                "你正在 DBB3 唯一控制面的服务端托管工作流中。",
                f"你的 Profile：{reviewer_profile}",
                f"官方 Kanban 根任务：{task_id}"
                if task_id and not remote_workers
                else "",
                reviewer_kanban_instruction,
                "你是结果审阅者。基于执行者结果做验收、风险检查和通过或退回判断。",
                "允许使用 Skill、MCP 和工具做必要的独立抽样复核，但不要完整重做整个任务。",
                "正常的 Skill、MCP、命令和取证调用不属于过度执行；只有明显超出用户目标、增加风险或无效成本时才指出越界。",
                "不要创建新的交付文件，也不要向用户做最终总结。",
                "结论最后单独一行写 HERMES_REVIEW: PASS 或 HERMES_REVIEW: REWORK。",
                f"用户任务：{content}",
                "执行者提交：",
                worker_result,
            )
            if item
        )
        if remote_workers:
            reviewer_result, reviewer_status, _reviewer_state = _run_hosted_remote_role(
                conversation_id,
                turn_id,
                profile=reviewer_profile,
                role_stage="reviewer",
                role_label=f"{reviewer_profile} · 审阅",
                prompt=reviewer_prompt,
                kanban_task_id=reviewer_task_scope,
                start_text="我已收到执行结果，正在独立验收证据与风险。",
                artifact_required=False,
                delivery_context=artifact_instruction,
                attachment_context=attachment_context,
            )
        else:
            reviewer_result, reviewer_status, _reviewer_state = _run_hosted_role(
                conversation_id,
                turn_id,
                profile=reviewer_profile,
                role_stage="reviewer",
                role_label=f"{reviewer_profile} · 审阅",
                prompt=reviewer_prompt,
                runner=runner,
                kanban_task_id=reviewer_task_scope,
                start_text="我已收到执行结果，正在独立验收证据与风险。",
                previous_state=(run.get("role_events") or {}).get("reviewer"),
            )
        _persist_hosted_turn(
            conversation_id,
            turn_id,
            patch={
                "reviewer_result": reviewer_result,
                "reviewer_status": reviewer_status,
                "stage": "reporter",
            },
        )
        if _finish_hosted_turn_if_cancelled(conversation_id, turn_id):
            return

    rework_round = int(run.get("rework_round") or 0)
    while (
        reviewer_status == "completed"
        and _review_requests_rework(reviewer_result)
        and rework_round < _HOSTED_REWORK_LIMIT
    ):
        active_rework_round = rework_round + 1
        _persist_hosted_turn(
            conversation_id,
            turn_id,
            patch={
                "active_rework_round": active_rework_round,
                "stage": "rework",
            },
            message={
                "role": "assistant",
                "name": reviewer_profile,
                "content": reviewer_result,
                "status": "completed",
                "kind": "message",
                "meta": {
                    "role_stage": f"reviewer:rework-request:{active_rework_round}",
                    "role_label": f"{reviewer_profile} · 退回返工",
                    "phase": "handoff",
                    "message_key": f"{turn_id}:reviewer:rework-request:{active_rework_round}",
                    "profile": reviewer_profile,
                    "handoff_to": worker_profiles,
                    "final_report": False,
                },
            },
        )
        if _finish_hosted_turn_if_cancelled(conversation_id, turn_id):
            return

        round_results: dict[str, str] = {}
        round_statuses: dict[str, str] = {}
        with ThreadPoolExecutor(
            max_workers=len(worker_profiles),
            thread_name_prefix=f"hosted-rework-{turn_id[-8:]}-{active_rework_round}",
        ) as executor:
            futures = {
                executor.submit(
                    execute_worker,
                    profile,
                    rework_feedback=reviewer_result,
                    rework_round=active_rework_round,
                ): profile
                for profile in worker_profiles
            }
            for future in as_completed(futures):
                profile, result, status, _worker_state = future.result()
                round_results[profile] = result
                round_statuses[profile] = status
        worker_results.update(round_results)
        worker_statuses.update(round_statuses)
        worker_result = "\n\n".join(
            f"## {profile}\n{worker_results.get(profile, '')}".strip()
            for profile in worker_profiles
        )
        worker_status = (
            "completed"
            if all(
                worker_statuses.get(profile) == "completed"
                for profile in worker_profiles
            )
            else "failed"
        )
        _persist_hosted_turn(
            conversation_id,
            turn_id,
            patch={
                "worker_result": worker_result,
                "worker_status": worker_status,
                "worker_results": dict(worker_results),
                "worker_statuses": dict(worker_statuses),
                "stage": "reviewer",
            },
        )
        reviewer_prompt = "\n".join(
            item
            for item in (
                "你正在 DBB3 唯一控制面的服务端托管工作流中。",
                f"你的 Profile：{reviewer_profile}",
                f"官方 Kanban 根任务：{task_id}"
                if task_id and not remote_workers
                else "",
                reviewer_kanban_instruction,
                f"这是第 {active_rework_round} 轮返工后的重新验收。",
                "逐项核对上轮退回意见和新的执行证据。",
                "不要创建新的交付文件，也不要向用户做最终总结。",
                "结论最后单独一行写 HERMES_REVIEW: PASS 或 HERMES_REVIEW: REWORK。",
                f"用户任务：{content}",
                "上轮退回意见：",
                reviewer_result,
                "返工后的执行者提交：",
                worker_result,
            )
            if item
        )
        if remote_workers:
            reviewer_result, reviewer_status, _reviewer_state = _run_hosted_remote_role(
                conversation_id,
                turn_id,
                profile=reviewer_profile,
                role_stage=f"reviewer:rework:{active_rework_round}",
                role_label=f"{reviewer_profile} · 返工复审",
                prompt=reviewer_prompt,
                kanban_task_id=reviewer_task_scope,
                start_text=f"第 {active_rework_round} 轮返工已提交，正在重新验收。",
                artifact_required=False,
                delivery_context=artifact_instruction,
                attachment_context=attachment_context,
                rework_round=active_rework_round,
            )
        else:
            reviewer_result, reviewer_status, _reviewer_state = _run_hosted_role(
                conversation_id,
                turn_id,
                profile=reviewer_profile,
                role_stage=f"reviewer:rework:{active_rework_round}",
                role_label=f"{reviewer_profile} · 返工复审",
                prompt=reviewer_prompt,
                runner=runner,
                kanban_task_id=reviewer_task_scope,
                start_text=f"第 {active_rework_round} 轮返工已提交，正在重新验收。",
            )
        rework_round = active_rework_round
        _persist_hosted_turn(
            conversation_id,
            turn_id,
            patch={
                "reviewer_result": reviewer_result,
                "reviewer_status": reviewer_status,
                "active_rework_round": 0,
                "rework_round": rework_round,
                "stage": "reporter",
            },
        )
        if _finish_hosted_turn_if_cancelled(conversation_id, turn_id):
            return

    if reviewer_status == "completed" and _review_requests_rework(reviewer_result):
        reviewer_status = "failed"
        reviewer_result = (
            f"{reviewer_result}\n\n"
            f"已达到 {_HOSTED_REWORK_LIMIT} 轮自动返工上限，任务保留为未通过。"
        )
        _persist_hosted_turn(
            conversation_id,
            turn_id,
            patch={
                "reviewer_result": reviewer_result,
                "reviewer_status": reviewer_status,
            },
        )

    reporter_result = str(run.get("reporter_result") or "")
    reporter_status = str(run.get("reporter_status") or "")
    if not reporter_result:
        reporter_prompt = "\n".join(
            item
            for item in (
                "你是这个服务端托管任务唯一的最终汇报者。",
                f"你的 Profile：{reporter_profile}",
                f"官方 Kanban 根任务：{task_id}"
                if task_id and not remote_workers
                else "",
                reporter_kanban_instruction,
                "综合执行者和审阅者的信息，只汇报一次完成状态、关键结果、证据、问题与下一步。",
                "不要重复执行工作，也不要重新生成执行者已经创建的文件。",
                f"用户任务：{content}",
                "执行者提交：",
                worker_result,
                "审阅者结论：",
                reviewer_result,
                artifact_instruction,
            )
            if item
        )
        if remote_workers:
            reporter_result, reporter_status, _reporter_state = _run_hosted_remote_role(
                conversation_id,
                turn_id,
                profile=reporter_profile,
                role_stage="reporter",
                role_label="Hermes · 最终汇报",
                prompt=reporter_prompt,
                kanban_task_id=reporter_task_scope,
                start_text="执行与审阅信息已齐，正在整理唯一的最终汇报。",
                artifact_required=False,
                delivery_context=artifact_instruction,
                attachment_context=attachment_context,
            )
        else:
            reporter_result, reporter_status, _reporter_state = _run_hosted_role(
                conversation_id,
                turn_id,
                profile=reporter_profile,
                role_stage="reporter",
                role_label="Hermes · 最终汇报",
                prompt=reporter_prompt,
                runner=runner,
                kanban_task_id=reporter_task_scope,
                start_text="执行与审阅信息已齐，正在整理唯一的最终汇报。",
                final_report=True,
                previous_state=(run.get("role_events") or {}).get("reporter"),
            )

    if _finish_hosted_turn_if_cancelled(conversation_id, turn_id):
        return

    attachments = []
    if artifact_required:
        attachments = _hosted_turn_output_attachments(
            conversation_id,
            turn_id,
            int(run.get("started_at") or 0),
        )
    missing_required_artifact = artifact_required and not attachments
    reporter_state_snapshot = (
        _reporter_state
        if "_reporter_state" in locals()
        else (run.get("role_events") or {}).get("reporter") or {}
    )
    final_status = (
        "completed"
        if (
            worker_status == reviewer_status == reporter_status == "completed"
            and not missing_required_artifact
        )
        else "failed"
    )
    if missing_required_artifact:
        reporter_result = "\n\n".join(
            item
            for item in (
                reporter_result,
                "The task required a deliverable file, but no verified file reached the account library.",
            )
            if item
        )
    now = int(time.time() * 1000)
    _persist_hosted_turn(
        conversation_id,
        turn_id,
        patch={
            "reporter_result": reporter_result,
            "reporter_status": reporter_status,
            "status": final_status,
            "stage": "completed" if final_status == "completed" else "failed",
            "completed_at": now,
            "notification": _completion_notification_record(
                conversation_id,
                turn_id,
                final_status,
                reporter_result,
            ),
        },
        message={
            "role": "assistant",
            "name": reporter_profile,
            "content": reporter_result,
            "status": final_status,
            "kind": "message",
            "meta": {
                "role_stage": "reporter",
                "role_label": "Hermes · 最终汇报",
                "collapse_activities": True,
                "final_report": True,
                "attachments": attachments,
                "task_id": task_id,
                "activities": list(reporter_state_snapshot.get("activities") or []),
                "actual_model": str(reporter_state_snapshot.get("actual_model") or ""),
                "actual_provider": str(
                    reporter_state_snapshot.get("actual_provider") or ""
                ),
            },
        },
    )
    _schedule_mobile_completion_notification(
        conversation_id,
        turn_id,
        final_status,
        reporter_result,
    )


def _next_hosted_turn_id(
    conversation_id: str,
    *,
    excluded: Optional[set[str]] = None,
) -> str:
    """Return the oldest durable unfinished turn for one conversation."""

    with _STATE_LOCK:
        state = load_single_state()
        conversation = next(
            (
                item
                for item in state.get("conversations") or []
                if isinstance(item, dict) and item.get("id") == conversation_id
            ),
            None,
        )
        # Deleting a conversation is also the cancellation boundary for its
        # serial hosted consumer. The consumer can race with deletion between
        # turns, so a missing record is a normal empty-queue result.
        if conversation is None:
            return ""
        candidates = [
            (
                int(run.get("created_at") or 0),
                index,
                str(candidate_turn_id),
            )
            for index, (candidate_turn_id, run) in enumerate(
                (conversation.get("hosted_turns") or {}).items()
            )
            if isinstance(run, dict)
            and str(run.get("status") or "queued") in {"queued", "running"}
            and str(candidate_turn_id) not in (excluded or set())
        ]
    if not candidates:
        return ""
    return min(candidates)[2]


def start_hosted_workflow(conversation_id: str, turn_id: str) -> threading.Thread:
    del turn_id  # The per-conversation consumer selects the oldest durable turn.
    key = str(conversation_id)
    with _HOSTED_THREADS_LOCK:
        existing = _HOSTED_THREADS.get(key)
        if existing is not None and existing.is_alive():
            return existing

        def run_and_release() -> None:
            attempted_turn_ids: set[str] = set()
            try:
                while True:
                    current_turn_id = _next_hosted_turn_id(
                        conversation_id,
                        excluded=attempted_turn_ids,
                    )
                    if not current_turn_id:
                        break
                    attempted_turn_ids.add(current_turn_id)
                    try:
                        with _hosted_conversation_execution_lock(conversation_id):
                            execute_hosted_workflow(
                                conversation_id,
                                current_turn_id,
                            )
                    except Exception as exc:
                        clean_error = sanitize_runtime_error(exc)
                        try:
                            _persist_hosted_turn(
                                conversation_id,
                                current_turn_id,
                                patch={
                                    "status": "failed",
                                    "stage": "failed",
                                    "error": clean_error,
                                    "completed_at": int(time.time() * 1000),
                                    "notification": _completion_notification_record(
                                        conversation_id,
                                        current_turn_id,
                                        "failed",
                                        clean_error,
                                    ),
                                },
                                message={
                                    "role": "assistant",
                                    "name": "default",
                                    "content": f"服务端托管任务失败：{clean_error}",
                                    "status": "failed",
                                    "kind": "message",
                                    "meta": {
                                        "role_stage": "reporter",
                                        "role_label": "Hermes · 最终汇报",
                                        "final_report": True,
                                    },
                                },
                            )
                        except Exception:
                            pass
                        _schedule_mobile_completion_notification(
                            conversation_id,
                            current_turn_id,
                            "failed",
                            clean_error,
                        )
            finally:
                with _HOSTED_THREADS_LOCK:
                    _HOSTED_THREADS.pop(key, None)
                try:
                    _finalize_pending_conversation_deletion(conversation_id)
                except Exception:
                    pass
                # A turn can be committed while the consumer is between its
                # final empty check and removal. Rescan after removal so that
                # this handoff window never strands durable queued work.
                try:
                    pending_turn_id = _next_hosted_turn_id(
                        conversation_id,
                        excluded=attempted_turn_ids,
                    )
                except Exception:
                    pending_turn_id = ""
                if pending_turn_id:
                    start_hosted_workflow(conversation_id, pending_turn_id)

        thread = threading.Thread(
            target=run_and_release,
            name=f"hermes-hosted-{conversation_id[-12:]}",
            daemon=True,
        )
        _HOSTED_THREADS[key] = thread
        thread.start()
        return thread


def _hosted_conversation_execution_lock(conversation_id: str) -> threading.Lock:
    with _HOSTED_CONVERSATION_LOCKS_LOCK:
        return _HOSTED_CONVERSATION_LOCKS.setdefault(
            str(conversation_id),
            threading.Lock(),
        )


def resume_unfinished_hosted_workflows(
    conversations: list[dict[str, Any]],
) -> None:
    for conversation in conversations:
        conversation_id = str(conversation.get("id") or "").strip()
        if not conversation_id:
            continue
        for turn_id, run in (conversation.get("hosted_turns") or {}).items():
            if not isinstance(run, dict):
                continue
            if run.get("status") in {"queued", "running"}:
                start_hosted_workflow(conversation_id, str(turn_id))
                continue
            notification = run.get("notification")
            if (
                isinstance(notification, dict)
                and str(notification.get("state") or "queued")
                not in _MOBILE_NOTIFICATION_TERMINAL_STATUSES
            ):
                _schedule_mobile_completion_notification(
                    conversation_id,
                    str(turn_id),
                    str(notification.get("task_status") or run.get("status") or "failed"),
                    str(notification.get("result") or ""),
                )


def _room_by_id(state: dict[str, Any], room_id: str) -> dict[str, Any]:
    for room in state.get("rooms") or []:
        if room.get("id") == room_id:
            return room
    raise HTTPException(status_code=404, detail="群聊不存在")


def _configured_legacy_owner_id() -> str:
    """Return the only account allowed to adopt pre-account local data."""

    explicit = os.environ.get("HERMES_LEGACY_OWNER_ID", "").strip()
    if explicit:
        return explicit[:512]
    return os.environ.get("HERMES_OWNER_EMAIL", "").strip().lower()[:512]


def _legacy_owner_claim_allowed(existing_owner: str, owner_id: str) -> bool:
    """Require an explicit server-side binding before migrating local data."""

    existing = str(existing_owner or "").strip()
    requested = str(owner_id or "").strip()
    if existing not in {"", LOCAL_OWNER_ID} or not requested:
        return False
    if requested == LOCAL_OWNER_ID:
        return True
    configured = _configured_legacy_owner_id()
    return bool(configured and hmac.compare_digest(configured, requested))


def _claim_legacy_rooms_in_state(
    state: dict[str, Any],
    owner_id: str,
    *,
    requested_room_id: str = "",
) -> bool:
    """Assign pre-account rooms once, without reopening claimed records."""

    normalized_owner = str(owner_id or "").strip()
    if not normalized_owner:
        return False
    if requested_room_id:
        requested_room = _room_by_id(state, requested_room_id)
        requested_owner = str(requested_room.get("owner_id") or "").strip()
        if requested_owner == normalized_owner:
            return False
        if not _legacy_owner_claim_allowed(requested_owner, normalized_owner):
            return False
    claimed = False
    for room in state.get("rooms") or []:
        if not isinstance(room, dict):
            continue
        existing_owner = str(room.get("owner_id") or "").strip()
        if existing_owner == normalized_owner:
            continue
        if not _legacy_owner_claim_allowed(existing_owner, normalized_owner):
            continue
        room["owner_id"] = normalized_owner
        claimed = True
    return claimed


def _owned_room_in_state(
    state: dict[str, Any],
    room_id: str,
    owner_id: str,
) -> dict[str, Any]:
    room = _room_by_id(state, room_id)
    existing_owner = str(room.get("owner_id") or "").strip()
    if existing_owner != owner_id:
        raise HTTPException(status_code=404, detail="Room not found")
    return room


def _room_conversation_in_state(
    room: dict[str, Any],
    single_state: dict[str, Any],
    owner_id: str,
) -> tuple[dict[str, Any], bool]:
    conversation_id = str(room.get("conversation_id") or "").strip()
    if conversation_id:
        mapped = next(
            (
                item
                for item in single_state.get("conversations") or []
                if isinstance(item, dict) and item.get("id") == conversation_id
            ),
            None,
        )
        if mapped is not None:
            return _owned_conversation_in_state(
                single_state,
                conversation_id,
                owner_id,
            )
    room_id = str(room.get("id") or uuid.uuid4().hex[:12])
    conversation = create_single_conversation(
        "default",
        str(room.get("name") or "Collaboration room"),
    )
    conversation["id"] = f"chat_room_{room_id.removeprefix('room_')}"
    conversation["owner_id"] = owner_id
    conversation["room_id"] = room_id
    conversation["source"] = "collaboration_room"
    conversation["messages"] = normalize_stored_conversation_messages(
        [
            item
            for item in room.get("messages") or []
            if isinstance(item, dict)
        ]
    )
    single_state.setdefault("conversations", []).insert(0, conversation)
    room["conversation_id"] = conversation["id"]
    room["messages"] = []
    return conversation, True


def _room_maps_to_deleting_conversation(
    room: dict[str, Any],
    single_state: dict[str, Any],
) -> bool:
    conversation_id = str(room.get("conversation_id") or "").strip()
    return bool(
        conversation_id
        and any(
            isinstance(item, dict)
            and item.get("id") == conversation_id
            and item.get("delete_requested")
            for item in single_state.get("conversations") or []
        )
    )


def _room_projection(
    room: dict[str, Any],
    single_state: dict[str, Any],
    *,
    summary: bool,
) -> dict[str, Any]:
    projected = dict(room)
    projected.pop("owner_id", None)
    room_owner = str(room.get("owner_id") or "").strip()
    conversation_id = str(room.get("conversation_id") or "").strip()
    conversation = next(
        (
            item
            for item in single_state.get("conversations") or []
            if isinstance(item, dict)
            and item.get("id") == conversation_id
            and not item.get("delete_requested")
            and str(item.get("owner_id") or room_owner).strip()
            in {room_owner, LOCAL_OWNER_ID}
        ),
        None,
    )
    messages = (
        list(conversation.get("messages") or [])
        if isinstance(conversation, dict)
        else list(room.get("messages") or [])
    )
    projected["messages"] = messages[-1:] if summary else messages
    projected["message_count"] = len(messages)
    if isinstance(conversation, dict):
        projected["hosted_turns"] = _public_hosted_turns(
            conversation.get("hosted_turns")
        )
        projected["updated_at"] = max(
            int(projected.get("updated_at") or 0),
            int(conversation.get("updated_at") or 0),
        )
    return projected


def _public_hosted_turn(run: dict[str, Any]) -> dict[str, Any]:
    """Remove server-only execution paths and baselines from an API response."""

    projected = dict(run)
    user_delivery_context = str(projected.pop("user_delivery_context", "") or "").strip()
    for key in (
        "output_dir",
        "output_baseline",
        "output_baseline_captured_at",
        "room_request",
    ):
        projected.pop(key, None)
    if user_delivery_context:
        projected["delivery_context"] = user_delivery_context
    else:
        projected.pop("delivery_context", None)
    remote_runs = projected.get("remote_runs")
    if isinstance(remote_runs, dict):
        projected["remote_runs"] = {
            str(role): {
                key: value
                for key, value in remote.items()
                if key not in {"delivery_context", "attachment_context"}
            }
            for role, remote in remote_runs.items()
            if isinstance(remote, dict)
        }
    return projected


def _public_hosted_turns(hosted_turns: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(hosted_turns, dict):
        return {}
    return {
        str(turn_id): _public_hosted_turn(run)
        for turn_id, run in hosted_turns.items()
        if isinstance(run, dict)
    }


def _public_conversation(conversation: dict[str, Any]) -> dict[str, Any]:
    projected = dict(conversation)
    projected["hosted_turns"] = _public_hosted_turns(
        conversation.get("hosted_turns")
    )
    return projected


def _conversation_by_id(
    state: dict[str, Any],
    conversation_id: str,
) -> dict[str, Any]:
    for conversation in state.get("conversations") or []:
        if conversation.get("id") == conversation_id:
            return conversation
    raise HTTPException(status_code=404, detail="单聊会话不存在")


def _owned_conversation_in_state(
    state: dict[str, Any],
    conversation_id: str,
    owner_id: str,
) -> tuple[dict[str, Any], bool]:
    """Resolve one conversation without disclosing another account's data."""

    conversation = _conversation_by_id(state, conversation_id)
    existing_owner = str(conversation.get("owner_id") or "").strip()
    if conversation.get("delete_requested"):
        raise HTTPException(status_code=404, detail="Conversation not found")
    if existing_owner == owner_id:
        return conversation, False
    if not _legacy_owner_claim_allowed(existing_owner, owner_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    conversation["owner_id"] = owner_id
    return conversation, True


def _native_activity_projection(
    activity: dict[str, Any],
    *,
    message_model: str = "",
    message_provider: str = "",
) -> dict[str, Any]:
    """Add the stable native activity contract while retaining legacy keys."""
    activity = _redact_sensitive(activity)
    projected = dict(activity)
    started_at = activity.get("started_at")
    completed_at = activity.get("completed_at") or activity.get("ended_at")
    duration_ms = activity.get("duration_ms")
    if duration_ms is None and isinstance(activity.get("duration"), (int, float)):
        duration_ms = round(float(activity["duration"]) * 1000)
    if duration_ms is None and isinstance(started_at, (int, float)) and isinstance(completed_at, (int, float)):
        duration_ms = max(0, int(completed_at) - int(started_at))
    kind = str(activity.get("kind") or activity.get("category") or "status")
    name = str(activity.get("name") or activity.get("summary") or kind)
    detail = activity.get("detail")
    if detail in (None, ""):
        detail = activity.get("output") or activity.get("preview") or activity.get("input") or ""
    projected.update(
        {
            "kind": kind,
            "status": str(activity.get("status") or "completed"),
            "summary": str(activity.get("summary") or name),
            "detail": detail,
            "tool_name": str(activity.get("tool_name") or (name if kind == "tool" else "")),
            "model": str(activity.get("model") or message_model),
            "provider": str(activity.get("provider") or message_provider),
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_ms": duration_ms,
            "duration": (
                round(float(duration_ms) / 1000, 3)
                if isinstance(duration_ms, (int, float))
                else activity.get("duration")
            ),
        }
    )
    return projected


def _project_native_message(message: dict[str, Any]) -> dict[str, Any]:
    """Mirror sender, role, time, model, handoff, and activities at top level."""
    meta = dict(message.get("meta") or {})
    canonical_role = str(message.get("role") or "assistant")
    default_stage = canonical_role if canonical_role in {"user", "system"} else "chat"
    stage = str(meta.get("base_role_stage") or meta.get("role_stage") or default_stage)
    base_stage = stage.split(":", 1)[0]
    logical_role = {
        "dispatch": "dispatcher",
        "workflow": "dispatcher",
        "worker": "worker",
        "reviewer": "reviewer",
        "reporter": "reporter",
        "chat": "hermes",
        "user": "user",
        "system": "system",
    }.get(base_stage, base_stage or "hermes")
    profile = str(meta.get("profile") or message.get("name") or "default")
    model = str(meta.get("actual_model") or message.get("model") or "")
    provider = str(meta.get("actual_provider") or message.get("provider") or "")
    activities = [
        _native_activity_projection(item, message_model=model, message_provider=provider)
        for item in meta.get("activities") or []
        if isinstance(item, dict)
    ]
    meta["activities"] = activities
    created_at = message.get("created_at") or int(time.time() * 1000)
    updated_at = message.get("updated_at") or created_at
    terminal = str(message.get("status") or "") in {
        "completed", "failed", "cancelled", "blocked"
    }
    handoff_to = meta.get("handoff_to") or []
    if isinstance(handoff_to, str):
        handoff_to = [handoff_to]
    completed_at = message.get("completed_at") or meta.get("completed_at")
    if terminal and completed_at is None:
        completed_at = updated_at
    message.update(
        {
            "sender_id": str(meta.get("sender_id") or profile or logical_role),
            "sender_name": str(meta.get("sender_name") or meta.get("role_label") or message.get("name") or profile),
            # Keep the canonical chat role (assistant/user/system) intact;
            # native clients read the participant role from sender_role.
            "role": canonical_role,
            "collaboration_role": logical_role,
            "sender_role": logical_role,
            "profile": profile,
            "avatar": str(meta.get("avatar") or f"role:{logical_role}"),
            "status": str(message.get("status") or "completed"),
            "model": model,
            "provider": provider,
            "handoff_to": list(handoff_to),
            "created_at": created_at,
            "updated_at": updated_at,
            "started_at": message.get("started_at") or meta.get("started_at") or created_at,
            "completed_at": completed_at,
            "activity_count": len(activities),
            "activities": activities,
            "meta": meta,
        }
    )
    return message


def _append_message(
    room: dict[str, Any],
    *,
    role: str,
    name: str,
    content: str,
    status: str = "completed",
    kind: str = "message",
    meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    message = {
        "id": f"msg_{uuid.uuid4().hex[:14]}",
        "role": role,
        "name": name,
        "content": content,
        "status": status,
        "kind": kind,
        "created_at": int(time.time() * 1000),
    }
    if meta:
        message["meta"] = meta
    _project_native_message(message)
    messages = room.setdefault("messages", [])
    messages.append(message)
    room["updated_at"] = message["created_at"]
    return message


class CreateRoomBody(BaseModel):
    name: str = "新群聊"
    profiles: list[str] = Field(default_factory=list)


class SendMessageBody(BaseModel):
    content: str
    profiles: Optional[list[str]] = None
    request_id: str = ""
    turn_id: str = ""


class CreateSingleConversationBody(BaseModel):
    profile: str = "default"
    client_id: str = ""
    title: str = "新对话"


class RenameSingleConversationBody(BaseModel):
    title: str


class AdoptSingleConversationBody(BaseModel):
    profile: str = "default"
    session_id: str
    title: str = "Imported session"
    messages: list[dict[str, Any]] = Field(default_factory=list)


class SendSingleMessageBody(BaseModel):
    content: str


class RouteMessageBody(BaseModel):
    content: str
    mode: str = "auto"
    recent_messages: list[dict[str, Any]] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class RecordMessageBody(BaseModel):
    role: str
    name: str
    content: str
    status: str = "completed"
    kind: str = "message"
    meta: dict[str, Any] = Field(default_factory=dict)


class RuntimeSessionBody(BaseModel):
    profile: str = "default"
    session_id: str
    turn_id: str = ""
    status: str = "running"


class HostedTurnBody(BaseModel):
    turn_id: str
    content: str
    title: str = ""
    profiles: list[str] = Field(default_factory=list)
    artifact_required: bool = False
    attachment_ids: list[str] = Field(default_factory=list)
    attachment_context: str = ""
    delivery_context: str = ""
    mode: str = "work"
    route_metadata: dict[str, Any] = Field(default_factory=dict)


class EnqueueHostedTurnBody(BaseModel):
    request_id: str
    turn_id: str
    message: dict[str, Any]
    recent_messages: list[dict[str, Any]] = Field(default_factory=list)
    profiles: list[str] = Field(default_factory=list)
    attachment_ids: list[str] = Field(default_factory=list)
    attachment_context: str = ""
    delivery_context: str = ""


class HostedTurnCancellationBody(BaseModel):
    reason: str = "用户取消"


class ConnectorPullBody(BaseModel):
    connector_id: str = "dbb3-primary"
    limit: int = 5
    lease_seconds: int = _REMOTE_RUN_LEASE_SECONDS


class ConnectorAckBody(BaseModel):
    connector_id: str = "dbb3-primary"
    idempotency_key: str = ""
    remote_task_id: str = ""
    root_task_id: str = ""
    session_id: str = ""
    accepted_at: str = ""
    lease_seconds: int = _REMOTE_RUN_LEASE_SECONDS


class ConnectorStatusBody(BaseModel):
    connector_id: str = "dbb3-primary"
    checkpoint_cursor: int = 0
    status: str = "running"
    terminal: bool = False
    summary: str = ""
    result: str = ""
    error: str = ""
    activities: list[dict[str, Any]] = Field(default_factory=list)
    remote_task_id: str = ""
    root_task_id: str = ""
    session_id: str = ""
    actual_model: str = ""
    actual_provider: str = ""
    observed_at: str = ""


class ConnectorCancelAckBody(BaseModel):
    connector_id: str = "dbb3-primary"
    checkpoint_cursor: int = 0
    summary: str = ""
    observed_at: str = ""


def _validate_connector_claim(claimed: Any, authenticated: str) -> None:
    normalized = str(claimed or "").strip()[:128]
    if normalized and normalized != authenticated:
        raise HTTPException(status_code=403, detail="Connector identity mismatch")


def _validate_connector_batch(
    body: ConnectorPullBody,
    authenticated: str,
) -> tuple[str, int, int]:
    _validate_connector_claim(body.connector_id, authenticated)
    limit = max(1, min(int(body.limit or 1), _MAX_REMOTE_RUNS_PER_PULL))
    lease_seconds = max(5, min(int(body.lease_seconds or _REMOTE_RUN_LEASE_SECONDS), 900))
    return authenticated, limit, lease_seconds


def _remote_run_for_connector(
    request: Request,
    remote_run_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    connector_id = _require_connector(request)
    with _STATE_LOCK:
        state = load_single_state()
        location = _remote_run_location(state, remote_run_id)
        if location is None:
            raise HTTPException(status_code=404, detail="Remote run not found")
        conversation, hosted, _role_key, remote_run = location
        if _remote_run_connector_id(remote_run) != connector_id:
            raise HTTPException(status_code=404, detail="Remote run not found")
        return conversation, hosted, remote_run


def _remote_run_connector_payload(remote_run: dict[str, Any]) -> dict[str, Any]:
    payload = _remote_run_public(remote_run)
    payload["remote_run_id"] = payload.pop("id", "")
    return payload


def _sanitize_remote_activities(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    sanitized: list[dict[str, Any]] = []
    for item in value[:200]:
        if not isinstance(item, dict):
            continue
        clean = _redact_sensitive(dict(item))
        for key in ("output", "detail", "result", "input", "args", "summary"):
            if key in clean and isinstance(clean[key], str):
                clean[key] = clean[key][:12000]
        sanitized.append(clean)
    return sanitized


def _apply_remote_checkpoint(
    remote_run_id: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    payload = _redact_sensitive(payload)
    status = str(payload.get("status") or "").strip().lower()
    terminal = bool(payload.get("terminal"))
    if status not in {"running", "completed", "failed", "cancelled"}:
        raise HTTPException(status_code=422, detail="Invalid remote run status")
    expected_terminal = status in _REMOTE_TERMINAL_STATUSES
    if terminal != expected_terminal:
        raise HTTPException(status_code=422, detail="terminal does not match status")
    try:
        cursor = int(payload.get("checkpoint_cursor") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="checkpoint_cursor must be an integer") from exc
    if cursor < 0:
        raise HTTPException(status_code=422, detail="checkpoint_cursor must be non-negative")

    with _STATE_LOCK:
        state = load_single_state()
        location = _remote_run_location(state, remote_run_id)
        if location is None:
            raise HTTPException(status_code=404, detail="Remote run not found")
        conversation, hosted, role_key, remote_run = location
        current_status = str(remote_run.get("status") or "queued")
        current_cursor = int(remote_run.get("checkpoint_cursor") or 0)
        if current_status in _REMOTE_TERMINAL_STATUSES:
            # Completion and cancellation can cross in flight. Once terminal,
            # every later checkpoint is an idempotent observation of the
            # stored result rather than a contract error that kills a systemd
            # connector configured not to restart on protocol failures.
            return dict(remote_run), False
        if (
            status == "completed"
            and bool(remote_run.get("artifact_required"))
            and not any(
                isinstance(item, dict) and item.get("id")
                for item in remote_run.get("artifacts") or []
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="Required artifact must be uploaded before completion",
            )
        if cursor < current_cursor:
            return dict(remote_run), False
        now = int(time.time() * 1000)
        remote_run.update(
            {
                "status": status,
                "checkpoint_cursor": cursor,
                "summary": str(payload.get("summary") or "")[:20000],
                "result": str(payload.get("result") or "")[:50000],
                "error": str(payload.get("error") or "")[:4000],
                "activities": _sanitize_remote_activities(payload.get("activities")),
                "updated_at": now,
            }
        )
        for key in ("remote_task_id", "root_task_id", "session_id", "actual_model", "actual_provider"):
            value = str(payload.get(key) or "").strip()
            if value:
                remote_run[key] = value[:512]
        observed_at = str(payload.get("observed_at") or "").strip()
        if observed_at:
            remote_run["observed_at"] = observed_at[:128]
        if status == "running":
            remote_run.setdefault("started_at", now)
        if expected_terminal:
            remote_run["completed_at"] = now
            remote_run.pop("lease_until", None)
            remote_run.pop("lease_owner", None)
        if status == "cancelled":
            remote_run["cancel_requested"] = True
        save_single_state(state)
        persisted = dict(remote_run)
        conversation_id = str(conversation.get("id") or "")
        turn_id = str(hosted.get("turn_id") or "")
        role_label = f"{remote_run.get('profile') or role_key} · 执行"
    _notify_hosted_update()
    _remote_run_state_message(
        conversation_id,
        turn_id,
        persisted,
        role_label=role_label,
    )
    if expected_terminal:
        _finalize_pending_conversation_deletion(conversation_id)
    return persisted, True


def _connector_relative_path(value: str) -> str:
    decoded = unquote(str(value or "").replace("\x00", "")).replace("\\", "/")
    if not decoded or decoded.startswith("/") or re.match(r"^[A-Za-z]:", decoded):
        raise HTTPException(status_code=422, detail="relative_path must be relative")
    parts = [part for part in decoded.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise HTTPException(status_code=422, detail="relative_path is invalid")
    return "/".join(parts)[:1024]


@router.get("/connector/health")
def connector_health(request: Request):
    connector_id = _require_connector(request)
    return {
        "ok": True,
        "connector_id": connector_id,
        "contract_version": _REMOTE_CONTRACT_VERSION,
        "server_time": int(time.time() * 1000),
        "max_artifact_bytes": _MAX_ATTACHMENT_BYTES,
        "capabilities": [
            "pull",
            "ack",
            "checkpoint",
            "cancel",
            "artifact-upload",
            "attachment-download",
        ],
    }


def _connector_run_attachments(
    conversation: dict[str, Any],
    hosted: dict[str, Any],
) -> list[dict[str, Any]]:
    owner_id = str(conversation.get("owner_id") or LOCAL_OWNER_ID)
    records: list[dict[str, Any]] = []
    for file_id in list(dict.fromkeys(hosted.get("attachment_ids") or []))[:32]:
        record = _file_library().get_file(owner_id, str(file_id))
        if record is None or record.get("status") != "available":
            continue
        records.append(
            {
                "id": str(record.get("id") or ""),
                "name": str(record.get("name") or "attachment"),
                "sha256": str(record.get("sha256") or ""),
                "mime_type": str(
                    record.get("mime_type") or "application/octet-stream"
                ),
                "size": int(record.get("size") or 0),
                "download_url": (
                    "/connector/runs/"
                    f"{quote(str(hosted.get('turn_id') or ''), safe='')}"
                    f"/attachments/{quote(str(record.get('id') or ''), safe='')}"
                ),
            }
        )
    return records


@router.get("/connector/runs/{remote_run_id}/attachments")
def connector_list_run_attachments(remote_run_id: str, request: Request):
    conversation, hosted, _remote_run = _remote_run_for_connector(
        request,
        remote_run_id,
    )
    attachments = _connector_run_attachments(conversation, hosted)
    for attachment in attachments:
        attachment["download_url"] = (
            f"/connector/runs/{quote(remote_run_id, safe='')}"
            f"/attachments/{quote(str(attachment['id']), safe='')}"
        )
    return {"attachments": attachments}


@router.get("/connector/runs/{remote_run_id}/attachments/{file_id}")
def connector_download_run_attachment(
    remote_run_id: str,
    file_id: str,
    request: Request,
):
    conversation, hosted, _remote_run = _remote_run_for_connector(
        request,
        remote_run_id,
    )
    allowed = {
        str(item)
        for item in hosted.get("attachment_ids") or []
        if str(item).startswith("file_")
    }
    if file_id not in allowed:
        raise HTTPException(status_code=404, detail="Attachment not found")
    owner_id = str(conversation.get("owner_id") or LOCAL_OWNER_ID)
    try:
        record, path = _file_library().resolve_download(owner_id, file_id)
    except (KeyError, FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Attachment not found") from exc
    return FileResponse(
        path=str(path),
        filename=str(record["name"]),
        media_type=str(record["mime_type"]),
        headers={
            "Cache-Control": "private, no-store",
            "ETag": f'"{record["sha256"]}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/connector/runs/pull")
def connector_pull_runs(payload: ConnectorPullBody, request: Request):
    authenticated = _require_connector(request)
    connector_id, limit, lease_seconds = _validate_connector_batch(payload, authenticated)
    now = int(time.time() * 1000)
    lease_until = now + lease_seconds * 1000
    selected: list[dict[str, Any]] = []
    changed = False
    with _STATE_LOCK:
        state = load_single_state()
        for conversation in state.get("conversations") or []:
            if not isinstance(conversation, dict):
                continue
            for hosted in (conversation.get("hosted_turns") or {}).values():
                if not isinstance(hosted, dict):
                    continue
                for remote_run in (hosted.get("remote_runs") or {}).values():
                    if not isinstance(remote_run, dict):
                        continue
                    profile = str(remote_run.get("profile") or "")
                    if profile not in {"dbb3-worker", "pc-worker", "reviewer", "default"}:
                        continue
                    if _remote_run_connector_id(remote_run) != connector_id:
                        continue
                    status = str(remote_run.get("status") or "queued")
                    old_lease = int(remote_run.get("lease_until") or 0)
                    if status not in {"queued", "leased", "running"}:
                        continue
                    if status in {"leased", "running"} and old_lease > now:
                        continue
                    remote_run.update(
                        {
                            # A running item must remain visibly running while
                            # the connector renews its lease to poll terminal
                            # Kanban state. Only pre-ack work is marked leased.
                            "status": (
                                "running" if status == "running" else "leased"
                            ),
                            "lease_owner": connector_id,
                            "lease_until": lease_until,
                            "updated_at": now,
                        }
                    )
                    selected.append(_remote_run_connector_payload(remote_run))
                    changed = True
                    if len(selected) >= limit:
                        break
                if len(selected) >= limit:
                    break
            if len(selected) >= limit:
                break
        if changed:
            save_single_state(state)
    if changed:
        _notify_hosted_update()
    return {"runs": selected, "server_time": now}


@router.post("/connector/runs/{remote_run_id}/ack")
def connector_ack_run(remote_run_id: str, payload: ConnectorAckBody, request: Request):
    conversation, hosted, remote_run = _remote_run_for_connector(request, remote_run_id)
    connector_id = _require_connector(request)
    _validate_connector_claim(payload.connector_id, connector_id)
    expected_key = str(remote_run.get("idempotency_key") or "")
    supplied_key = str(payload.idempotency_key or "")
    if supplied_key and supplied_key != expected_key:
        raise HTTPException(status_code=409, detail="idempotency key mismatch")
    supplied_task = str(payload.remote_task_id or "").strip()
    existing_task = str(remote_run.get("remote_task_id") or "").strip()
    if supplied_task and existing_task and supplied_task != existing_task:
        raise HTTPException(status_code=409, detail="remote task mismatch")
    now = int(time.time() * 1000)
    with _STATE_LOCK:
        state = load_single_state()
        location = _remote_run_location(state, remote_run_id)
        if location is None:
            raise HTTPException(status_code=404, detail="Remote run not found")
        _conversation, _hosted, _role_key, record = location
        if str(record.get("status") or "") in _REMOTE_TERMINAL_STATUSES:
            return {"run": _remote_run_connector_payload(record), "applied": False}
        record.update(
            {
                "status": "running",
                "started_at": int(record.get("started_at") or now),
                "updated_at": now,
                "lease_owner": connector_id,
                "lease_until": now + max(5, min(int(payload.lease_seconds or _REMOTE_RUN_LEASE_SECONDS), 900)) * 1000,
            }
        )
        for key in ("remote_task_id", "root_task_id", "session_id"):
            value = str(getattr(payload, key) or "").strip()
            if value:
                record[key] = value[:512]
        save_single_state(state)
        persisted = dict(record)
    _notify_hosted_update()
    _remote_run_state_message(
        str(conversation.get("id") or ""),
        str(hosted.get("turn_id") or ""),
        persisted,
        role_label=f"{persisted.get('profile') or 'worker'} · 执行",
    )
    return {"run": _remote_run_connector_payload(persisted), "applied": True}


@router.post("/connector/runs/{remote_run_id}/status")
def connector_status_run(remote_run_id: str, payload: ConnectorStatusBody, request: Request):
    connector_id = _require_connector(request)
    _remote_run_for_connector(request, remote_run_id)
    _validate_connector_claim(payload.connector_id, connector_id)
    persisted, applied = _apply_remote_checkpoint(remote_run_id, payload.model_dump())
    return {"run": _remote_run_connector_payload(persisted), "applied": applied}


@router.post("/connector/runs/{remote_run_id}/fail")
def connector_fail_run(remote_run_id: str, payload: ConnectorStatusBody, request: Request):
    connector_id = _require_connector(request)
    _remote_run_for_connector(request, remote_run_id)
    _validate_connector_claim(payload.connector_id, connector_id)
    body = payload.model_dump()
    body.update({"status": "failed", "terminal": True})
    persisted, applied = _apply_remote_checkpoint(remote_run_id, body)
    return {"run": _remote_run_connector_payload(persisted), "applied": applied}


@router.post("/connector/cancellations/pull")
def connector_pull_cancellations(payload: ConnectorPullBody, request: Request):
    authenticated = _require_connector(request)
    connector_id, limit, lease_seconds = _validate_connector_batch(payload, authenticated)
    now = int(time.time() * 1000)
    selected: list[dict[str, Any]] = []
    changed = False
    with _STATE_LOCK:
        state = load_single_state()
        for conversation in state.get("conversations") or []:
            if not isinstance(conversation, dict):
                continue
            for hosted in (conversation.get("hosted_turns") or {}).values():
                if not isinstance(hosted, dict) or not hosted.get("cancel_requested"):
                    continue
                for remote_run in (hosted.get("remote_runs") or {}).values():
                    if not isinstance(remote_run, dict) or str(remote_run.get("status") or "") in _REMOTE_TERMINAL_STATUSES:
                        continue
                    if _remote_run_connector_id(remote_run) != connector_id:
                        continue
                    old_lease = int(remote_run.get("cancel_lease_until") or 0)
                    if old_lease > now:
                        continue
                    remote_run["cancel_lease_owner"] = connector_id
                    remote_run["cancel_lease_until"] = now + lease_seconds * 1000
                    remote_run["updated_at"] = now
                    selected.append(
                        {
                            "remote_run_id": remote_run.get("id"),
                            "remote_task_id": remote_run.get("remote_task_id", ""),
                            "root_task_id": remote_run.get("root_task_id", ""),
                            "checkpoint_cursor": int(
                                remote_run.get("checkpoint_cursor") or 0
                            ),
                            "reason": hosted.get("cancel_reason") or "用户取消",
                            "requested_at": hosted.get("cancel_requested_at") or now,
                        }
                    )
                    changed = True
                    if len(selected) >= limit:
                        break
                if len(selected) >= limit:
                    break
            if len(selected) >= limit:
                break
        if changed:
            save_single_state(state)
    return {"cancellations": selected, "server_time": now}


@router.post("/connector/runs/{remote_run_id}/cancel-ack")
def connector_cancel_ack(remote_run_id: str, payload: ConnectorCancelAckBody, request: Request):
    connector_id = _require_connector(request)
    _remote_run_for_connector(request, remote_run_id)
    _validate_connector_claim(payload.connector_id, connector_id)
    body = payload.model_dump()
    body.update({"status": "cancelled", "terminal": True})
    persisted, applied = _apply_remote_checkpoint(remote_run_id, body)
    return {"run": _remote_run_connector_payload(persisted), "applied": applied}


@router.post("/connector/runs/{remote_run_id}/artifacts")
async def connector_upload_artifact(remote_run_id: str, request: Request):
    _require_connector(request)
    header_id = str(request.headers.get("x-remote-run-id") or "").strip()
    if header_id != remote_run_id:
        raise HTTPException(status_code=422, detail="X-Remote-Run-ID mismatch")
    conversation, hosted, remote_run = _remote_run_for_connector(request, remote_run_id)
    relative_path = _connector_relative_path(request.headers.get("x-relative-path", ""))
    try:
        filename = safe_attachment_name(unquote(request.headers.get("x-filename", "")) or Path(relative_path).name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    expected_sha = str(request.headers.get("x-content-sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise HTTPException(status_code=422, detail="X-Content-SHA256 must be a SHA-256 hex digest")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_ATTACHMENT_BYTES:
                raise HTTPException(status_code=413, detail="附件不能超过 64 MB")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid Content-Length") from exc
    temp_root = Path(get_hermes_home()) / "collaboration" / "connector-upload-tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    temp = temp_root / f".{uuid.uuid4().hex}.upload"
    digest = hashlib.sha256()
    total = 0
    try:
        with temp.open("xb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > _MAX_ATTACHMENT_BYTES:
                    raise HTTPException(status_code=413, detail="附件不能超过 64 MB")
                digest.update(chunk)
                handle.write(chunk)
        actual_sha = digest.hexdigest()
        if actual_sha != expected_sha:
            raise HTTPException(status_code=422, detail="Artifact SHA-256 mismatch")
        owner_id = str(conversation.get("owner_id") or LOCAL_OWNER_ID)
        origin_key = f"remote:{remote_run_id}:{relative_path}:{actual_sha}"
        record = _file_library().ingest_file(
            owner_id,
            temp,
            name=filename,
            source="model_output",
            conversation_id=str(conversation.get("id") or ""),
            turn_id=str(hosted.get("turn_id") or ""),
            profile=str(remote_run.get("profile") or ""),
            origin_key=origin_key,
            mime_type=request.headers.get("content-type", ""),
            allowed_roots=[temp_root],
        )
    finally:
        temp.unlink(missing_ok=True)
    attachment = _library_attachment(record)
    now = int(time.time() * 1000)
    with _STATE_LOCK:
        state = load_single_state()
        location = _remote_run_location(state, remote_run_id)
        if location is not None:
            _conversation, _hosted, _role_key, current = location
            artifacts = current.get("artifacts")
            if not isinstance(artifacts, list):
                artifacts = []
                current["artifacts"] = artifacts
            if not any(str(item.get("id") or "") == str(record.get("id") or "") for item in artifacts if isinstance(item, dict)):
                artifacts.append(attachment)
            current["updated_at"] = now
            save_single_state(state)
    _persist_hosted_turn(
        str(conversation.get("id") or ""),
        str(hosted.get("turn_id") or ""),
        message={
            "role": "assistant",
            "name": str(remote_run.get("profile") or "worker"),
            "content": f"已收到交付文件：{filename}",
            "status": "completed",
            "kind": "message",
            "meta": {
                "role_stage": f"{remote_run.get('role_stage') or 'worker'}.artifact",
                "phase": "milestone",
                "message_key": f"{remote_run_id}:artifact:{record.get('id')}",
                "profile": str(remote_run.get("profile") or "worker"),
                "attachments": [attachment],
                "collapse_activities": True,
                "final_report": False,
            },
        },
    )
    return {"artifact": attachment, "applied": True}


@router.get("/profiles")
def get_profiles():
    return {"profiles": available_profiles()}


@router.post("/route")
def route_message(payload: RouteMessageBody):
    mode = payload.mode.strip().lower()
    if mode in {"chat", "work"}:
        routed = classify_user_intent(payload.content)
        routed["mode"] = mode
        routed["label"] = "简单任务" if mode == "chat" else "群聊 + 工作流"
        routed["confidence"] = 1.0
        routed["source"] = "manual"
        routed["reason"] = (
            "用户手动选择普通对话。"
            if mode == "chat"
            else "用户手动选择工作任务。"
        )
        if mode == "chat":
            routed["profiles"] = ["default"]
        elif routed.get("profiles") == ["default"]:
            routed["profiles"] = ["default", "dbb3-worker", "reviewer"]
        return routed
    model_calls = 0

    def model_classifier(content: str) -> Optional[dict[str, Any]]:
        nonlocal model_calls
        result = classify_intent_with_context_model(
            content,
            recent_messages=payload.recent_messages,
            attachments=payload.attachments,
            adjudicate=model_calls > 0,
        )
        model_calls += 1
        return result

    return classify_user_intent(payload.content, model_classifier=model_classifier)


_HOSTED_TURN_INDEX_FIELDS = (
    "turn_id",
    "status",
    "stage",
    "started_at",
    "updated_at",
    "completed_at",
    "task_id",
    "cancel_requested",
)


def compact_hosted_turns_for_index(
    hosted_turns: Any,
) -> dict[str, dict[str, Any]]:
    if not isinstance(hosted_turns, dict):
        return {}
    return {
        str(turn_id): {
            key: run.get(key)
            for key in _HOSTED_TURN_INDEX_FIELDS
            if key in run
        }
        for turn_id, run in hosted_turns.items()
        if isinstance(run, dict)
    }


@router.get("/single/conversations")
def get_single_conversations(request: Request = None):
    owner_id = owner_id_from_request(request)
    with _STATE_LOCK:
        state = load_single_state()
        conversations = state.get("conversations") or []
        owned: list[dict[str, Any]] = []
        changed = False
        for conversation in conversations:
            existing_owner = str(conversation.get("owner_id") or "").strip()
            if conversation.get("delete_requested"):
                continue
            if existing_owner != owner_id and _legacy_owner_claim_allowed(
                existing_owner,
                owner_id,
            ):
                conversation["owner_id"] = owner_id
                existing_owner = owner_id
                changed = True
            if existing_owner != owner_id:
                continue
            changed = reconcile_conversation_runtime_results(conversation) or changed
            changed = compact_conversation_title(conversation) or changed
            owned.append(conversation)
        if changed:
            save_single_state(state)
        summaries = [
            {
                **conversation,
                "messages": (conversation.get("messages") or [])[-1:],
                "message_count": len(conversation.get("messages") or []),
                "hosted_turns": compact_hosted_turns_for_index(
                    conversation.get("hosted_turns")
                ),
            }
            for conversation in owned
        ]
    resume_unfinished_hosted_workflows(owned)
    return {"conversations": summaries}


@router.post("/single/conversations")
def create_single_chat(payload: CreateSingleConversationBody, request: Request = None):
    known = {item["name"] for item in available_profiles()}
    if payload.profile not in known:
        raise HTTPException(status_code=400, detail="Hermes Profile 不存在")
    owner_id = owner_id_from_request(request)
    client_id = payload.client_id.strip()
    if client_id and not re.fullmatch(r"chat_[A-Za-z0-9._:-]{8,251}", client_id):
        raise HTTPException(status_code=422, detail="Invalid client_id")
    with _STATE_LOCK:
        state = load_single_state()
        if client_id:
            existing = next(
                (
                    item
                    for item in state.get("conversations") or []
                    if item.get("id") == client_id
                ),
                None,
            )
            if isinstance(existing, dict):
                existing_owner = str(existing.get("owner_id") or LOCAL_OWNER_ID)
                if existing_owner != owner_id:
                    raise HTTPException(status_code=404, detail="Conversation not found")
                return {
                    "conversation": _public_conversation(existing),
                    "created": False,
                }
        conversation = create_single_conversation(payload.profile, payload.title)
        if client_id:
            conversation["id"] = client_id
        conversation["owner_id"] = owner_id
        state["conversations"].insert(0, conversation)
        save_single_state(state)
    return {"conversation": _public_conversation(conversation), "created": True}


@router.post("/single/conversations/adopt")
def adopt_single_chat(
    payload: AdoptSingleConversationBody,
    request: Request = None,
):
    session_id = payload.session_id.strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="Session ID is required")
    known = {item["name"] for item in available_profiles()}
    if payload.profile not in known:
        raise HTTPException(status_code=400, detail="Hermes Profile does not exist")
    owner_id = owner_id_from_request(request)
    with _STATE_LOCK:
        state = load_single_state()
        for conversation in state.get("conversations") or []:
            runtime_sessions = conversation.get("runtime_sessions") or {}
            if runtime_sessions.get(payload.profile) == session_id:
                existing_owner = str(conversation.get("owner_id") or "").strip()
                if conversation.get("delete_requested"):
                    raise HTTPException(status_code=404, detail="Conversation not found")
                if existing_owner != owner_id and not _legacy_owner_claim_allowed(
                    existing_owner,
                    owner_id,
                ):
                    raise HTTPException(status_code=404, detail="Conversation not found")
                if existing_owner != owner_id:
                    conversation["owner_id"] = owner_id
                    save_single_state(state)
                return {
                    "conversation": _public_conversation(conversation),
                    "created": False,
                }
        conversation = create_adopted_single_conversation(
            payload.profile,
            session_id,
            payload.title,
            payload.messages,
        )
        conversation["owner_id"] = owner_id
        state["conversations"].insert(0, conversation)
        save_single_state(state)
    return {"conversation": _public_conversation(conversation), "created": True}


@router.get("/single/conversations/{conversation_id}")
def get_single_conversation(
    conversation_id: str,
    request: Request = None,
):
    owner_id = owner_id_from_request(request)
    with _STATE_LOCK:
        state = load_single_state()
        conversation, claimed = _owned_conversation_in_state(
            state,
            conversation_id,
            owner_id,
        )
        changed = claimed or reconcile_conversation_runtime_results(conversation)
        changed = reconcile_conversation_mapped_sessions(conversation) or changed
        changed = compact_conversation_title(conversation) or changed
        if changed:
            save_single_state(state)
        result = {"conversation": _public_conversation(conversation)}
    resume_unfinished_hosted_workflows([conversation])
    return result


@router.patch("/single/conversations/{conversation_id}")
def rename_single_conversation(
    conversation_id: str,
    payload: RenameSingleConversationBody,
    request: Request = None,
):
    title = " ".join(payload.title.split()).strip()[:120]
    if not title:
        raise HTTPException(status_code=400, detail="Conversation title is required")
    with _STATE_LOCK:
        state = load_single_state()
        conversation, _claimed = _owned_conversation_in_state(
            state,
            conversation_id,
            owner_id_from_request(request),
        )
        conversation["title"] = title
        conversation["updated_at"] = int(time.time() * 1000)
        save_single_state(state)
        return {"conversation": _public_conversation(conversation)}


@router.get("/single/conversations/{conversation_id}/attachments")
def get_conversation_attachments(conversation_id: str, request: Request):
    owner_id, conversation = _owned_conversation(request, conversation_id)
    uploads_dir = _conversation_file_dir(conversation_id, "uploads")
    outputs_dir = _conversation_file_dir(conversation_id, "outputs")
    _sync_conversation_files(owner_id, conversation, uploads_dir, outputs_dir)
    return {
        "attachments": _conversation_library_attachments(
            owner_id,
            conversation_id,
        ),
    }


@router.post("/single/conversations/{conversation_id}/attachments")
async def upload_conversation_attachment(
    conversation_id: str,
    request: Request,
):
    owner_id, conversation = _owned_conversation(request, conversation_id)
    try:
        filename = safe_attachment_name(
            unquote(request.headers.get("x-filename", ""))
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    upload_id = str(request.headers.get("x-upload-id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,256}", upload_id):
        raise HTTPException(status_code=422, detail="Invalid X-Upload-ID")
    uploads_dir = _conversation_file_dir(conversation_id, "uploads")
    total = 0
    origin_key = f"conversation-upload:{conversation_id}:{upload_id}"
    temp = uploads_dir / f".{filename}.{uuid.uuid4().hex}.upload"
    try:
        with temp.open("wb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > _MAX_ATTACHMENT_BYTES:
                    raise HTTPException(status_code=413, detail="附件不能超过 64 MB")
                handle.write(chunk)
        actual_sha = hashlib.sha256(temp.read_bytes()).hexdigest()
        existing = _file_library().get_file_by_origin(owner_id, origin_key)
        if (
            existing is not None
            and existing.get("status") == "available"
            and str(existing.get("sha256") or "") != actual_sha
        ):
            raise HTTPException(
                status_code=409,
                detail="X-Upload-ID was already used with different content",
            )
        record = _file_library().ingest_file(
            owner_id,
            temp,
            name=filename,
            source="user_upload",
            conversation_id=conversation_id,
            message_id=request.headers.get("x-message-id", ""),
            turn_id=request.headers.get("x-turn-id", ""),
            profile=request.headers.get("x-profile", "")
            or str(conversation.get("profile") or ""),
            origin_key=origin_key,
            mime_type=request.headers.get("content-type", ""),
            allowed_roots=[uploads_dir],
        )
    finally:
        temp.unlink(missing_ok=True)
    return {"attachment": _library_attachment(record)}


@router.get(
    "/single/conversations/{conversation_id}/attachments/"
    "{bucket}/{relative_path:path}"
)
def download_conversation_attachment(
    conversation_id: str,
    bucket: str,
    relative_path: str,
    request: Request,
    preview: bool = False,
):
    _owned_conversation(request, conversation_id)
    root = _conversation_file_dir(conversation_id, bucket).resolve()
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root):
        raise HTTPException(status_code=403, detail="附件路径越界")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="附件不存在")
    return FileResponse(
        path=str(target),
        filename=target.name,
        media_type=mimetypes.guess_type(target.name)[0]
        or "application/octet-stream",
        content_disposition_type="inline" if preview else "attachment",
    )


@router.post("/single/conversations/{conversation_id}/record")
def record_single_message(
    conversation_id: str,
    payload: RecordMessageBody,
    request: Request = None,
):
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")
    with _STATE_LOCK:
        state = load_single_state()
        conversation, _claimed = _owned_conversation_in_state(
            state,
            conversation_id,
            owner_id_from_request(request),
        )
        runtime_session_id = str(
            payload.meta.get("runtime_session_id") or ""
        ).strip()
        runtime_turn_id = str(
            payload.meta.get("runtime_turn_id") or ""
        ).strip()
        message = None
        if payload.role == "assistant" and (runtime_turn_id or runtime_session_id):
            message = next(
                (
                    item
                    for item in conversation.get("messages") or []
                    if isinstance(item.get("meta"), dict)
                    and (
                        item["meta"].get("runtime_turn_id") == runtime_turn_id
                        if runtime_turn_id
                        else item["meta"].get("runtime_session_id")
                        == runtime_session_id
                    )
                ),
                None,
            )
        if message is None:
            message = _append_message(
                conversation,
                role=payload.role,
                name=payload.name,
                content=payload.content.strip(),
                status=payload.status,
                kind=payload.kind,
                meta=payload.meta,
            )
        else:
            message.update(
                {
                    "role": payload.role,
                    "name": payload.name,
                    "content": payload.content.strip(),
                    "status": payload.status,
                    "kind": payload.kind,
                    "meta": {**message.get("meta", {}), **payload.meta},
                    "created_at": int(time.time() * 1000),
                }
            )
            conversation["updated_at"] = message["created_at"]
        if runtime_session_id:
            for run in (conversation.get("runtime_runs") or {}).values():
                same_turn = (
                    run.get("turn_id") == runtime_turn_id
                    if runtime_turn_id
                    else run.get("session_id") == runtime_session_id
                )
                if same_turn:
                    run["status"] = "completed"
                    run["completed_at"] = message["created_at"]
                    run["updated_at"] = message["created_at"]
        if (
            payload.role == "user"
            and conversation.get("title") in {"", "新对话", None}
        ):
            conversation["title"] = summarize_task_title(payload.content)
        save_single_state(state)
        owner_id = str(conversation.get("owner_id") or "").strip()
        profile = str(conversation.get("profile") or payload.name or "").strip()
        attachment_ids = _attachment_file_ids(message.get("meta"))
    if owner_id and attachment_ids:
        _file_library().update_links(
            owner_id,
            attachment_ids,
            conversation_id=conversation_id,
            message_id=str(message.get("id") or ""),
            turn_id=runtime_turn_id,
            profile=profile,
        )
    return {"message": message}


@router.post("/single/conversations/{conversation_id}/runtime-session")
def save_runtime_session(
    conversation_id: str,
    payload: RuntimeSessionBody,
    request: Request = None,
):
    with _STATE_LOCK:
        state = load_single_state()
        conversation, _claimed = _owned_conversation_in_state(
            state,
            conversation_id,
            owner_id_from_request(request),
        )
        if payload.status == "running":
            try:
                baseline_message_count = len(
                    _load_runtime_messages(payload.profile, payload.session_id)
                )
            except Exception:
                baseline_message_count = 0
            mark_conversation_runtime_run(
                conversation,
                payload.profile,
                payload.session_id,
                turn_id=payload.turn_id,
                baseline_message_count=baseline_message_count,
            )
        else:
            set_conversation_runtime_session(
                conversation,
                payload.profile,
                payload.session_id,
            )
        runtime_sessions = conversation.get("runtime_sessions") or {}
        save_single_state(state)
    return {"runtime_sessions": runtime_sessions}


@router.get("/single/conversations/{conversation_id}/hosted-events")
async def stream_hosted_conversation_events(
    conversation_id: str,
    request: Request,
):
    owner_id = owner_id_from_request(request)
    with _STATE_LOCK:
        state = load_single_state()
        _conversation, claimed = _owned_conversation_in_state(
            state,
            conversation_id,
            owner_id,
        )
        if claimed:
            save_single_state(state)

    async def event_stream():
        revision = -1
        while not await request.is_disconnected():
            with _STATE_LOCK:
                state = load_single_state()
                conversation, _claimed = _owned_conversation_in_state(
                    state,
                    conversation_id,
                    owner_id,
                )
                current_revision = _HOSTED_UPDATE_REVISION
                payload = json.dumps(
                    {"conversation": _public_conversation(conversation)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            if current_revision != revision:
                yield f"event: conversation\ndata: {payload}\n\n"
                revision = current_revision
            next_revision = await asyncio.to_thread(
                _wait_for_hosted_update,
                revision,
                15.0,
            )
            if next_revision == revision:
                yield ": keepalive\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _hosted_route_parameters(
    *,
    route_metadata: Any,
    content: str = "",
    requested_mode: str = "",
    requested_profiles: Optional[list[str]] = None,
    requested_artifact: bool = False,
) -> tuple[dict[str, Any], str, list[str], bool]:
    route = dict(route_metadata) if isinstance(route_metadata, dict) else {}
    mode = str(requested_mode or route.get("mode") or "work").strip().lower()
    if mode not in {"chat", "work"}:
        raise HTTPException(status_code=400, detail="mode must be chat or work")
    selected_profiles = list(requested_profiles or route.get("profiles") or [])
    if mode == "chat":
        selected_profile = next(
            (str(item).strip() for item in selected_profiles if str(item).strip()),
            "default",
        )
        known_profiles = {str(item.get("name") or "") for item in available_profiles()}
        if selected_profile not in known_profiles:
            raise HTTPException(status_code=400, detail="Hermes Profile does not exist")
        selected_profiles = [selected_profile]
        route["profiles"] = selected_profiles
    else:
        requested_workers = [
            profile
            for profile in selected_profiles
            if collaboration_role(profile) == "worker"
        ]
        worker_profiles, constraints = _constrained_worker_profiles(
            content,
            profiles=requested_workers,
            targets=[str(item).lower() for item in route.get("targets") or []],
        )
        if not worker_profiles:
            raise HTTPException(
                status_code=422,
                detail="No eligible worker target remains after applying placement constraints",
            )
        route["target_constraints"] = constraints
        route["targets"] = [
            profile.removesuffix("-worker") for profile in worker_profiles
        ]
        route["profiles"] = ["default", *worker_profiles, "reviewer"]
        selected_profiles = list(
            dict.fromkeys(["default", *worker_profiles, "reviewer"])
        )
    artifact = route.get("artifact")
    artifact = dict(artifact) if isinstance(artifact, dict) else {}
    artifact_required = bool(
        requested_artifact
        or route.get("artifact_required")
        or str(artifact.get("decision") or "").lower() == "required"
    )
    return route, mode, selected_profiles, artifact_required


def _enqueue_payload_fingerprint(
    payload: EnqueueHostedTurnBody,
    *,
    message_content: str,
) -> str:
    canonical = {
        "turn_id": payload.turn_id.strip(),
        "message": payload.message,
        "message_content": message_content,
        "recent_messages": payload.recent_messages,
        "attachment_ids": list(dict.fromkeys(payload.attachment_ids)),
        "attachment_context": payload.attachment_context,
        "delivery_context": payload.delivery_context,
    }
    return hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _enqueued_turn_response(
    conversation: dict[str, Any],
    request_record: dict[str, Any],
    *,
    replayed: bool,
) -> dict[str, Any]:
    messages = [
        item
        for item in conversation.get("messages") or []
        if isinstance(item, dict)
    ]
    message_id = str(request_record.get("message_id") or "")
    route_message_id = str(request_record.get("route_message_id") or "")
    user_message = next(
        (item for item in messages if str(item.get("id") or "") == message_id),
        {},
    )
    route_message = next(
        (
            item
            for item in messages
            if str(item.get("id") or "") == route_message_id
        ),
        {},
    )
    turn_id = str(request_record.get("turn_id") or "")
    hosted = (conversation.get("hosted_turns") or {}).get(turn_id)
    if not isinstance(hosted, dict):
        hosted = {}
    return {
        "accepted": True,
        "replayed": replayed,
        "request_id": str(request_record.get("request_id") or ""),
        "conversation_id": str(conversation.get("id") or ""),
        "message": user_message,
        "route": dict(request_record.get("route") or {}),
        "route_message": route_message or None,
        "hosted_turn": _public_hosted_turn(hosted),
    }


@router.post("/single/conversations/{conversation_id}/enqueue")
def enqueue_hosted_turn(
    conversation_id: str,
    payload: EnqueueHostedTurnBody,
    request: Request,
):
    request_id = payload.request_id.strip()[:256]
    turn_id = payload.turn_id.strip()[:256]
    if not request_id:
        raise HTTPException(status_code=400, detail="request_id is required")
    if not turn_id:
        raise HTTPException(status_code=400, detail="turn_id is required")
    message_source = dict(payload.message or {})
    message_content = _message_content_text(message_source)
    if not message_content:
        raise HTTPException(status_code=400, detail="消息不能为空")
    if str(message_source.get("role") or "user").strip().lower() != "user":
        raise HTTPException(status_code=422, detail="message.role must be user")
    attachment_ids = list(
        dict.fromkeys(str(item).strip() for item in payload.attachment_ids)
    )
    if len(attachment_ids) > 32 or any(
        not item.startswith("file_") for item in attachment_ids
    ):
        raise HTTPException(status_code=422, detail="Invalid attachment_ids")
    owner_id = owner_id_from_request(request)
    fingerprint = _enqueue_payload_fingerprint(
        payload,
        message_content=message_content,
    )

    route_attachments: list[dict[str, Any]] = []
    replay_response: Optional[dict[str, Any]] = None
    with _STATE_LOCK:
        state = load_single_state()
        conversation, _claimed = _owned_conversation_in_state(
            state,
            conversation_id,
            owner_id,
        )
        conversation_profile = str(conversation.get("profile") or "default").strip() or "default"
        requests = conversation.get("enqueue_requests")
        requests = requests if isinstance(requests, dict) else {}
        existing_request = requests.get(request_id)
        if isinstance(existing_request, dict):
            if str(existing_request.get("fingerprint") or "") != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="request_id was already used with a different payload",
                )
            replay_response = _enqueued_turn_response(
                conversation,
                existing_request,
                replayed=True,
            )
        else:
            for file_id in attachment_ids:
                file_record = _file_library().get_file(owner_id, file_id)
                if file_record is None or file_record.get("status") != "available":
                    raise HTTPException(status_code=404, detail="Attachment not found")
                route_attachments.append(
                    {
                        "id": file_id,
                        "name": str(file_record.get("name") or "attachment"),
                        "mime_type": str(
                            file_record.get("mime_type")
                            or "application/octet-stream"
                        ),
                        "size": int(file_record.get("size") or 0),
                    }
                )
    if replay_response is not None:
        hosted = replay_response.get("hosted_turn")
        if isinstance(hosted, dict) and str(hosted.get("status") or "") not in _HOSTED_TERMINAL_STATUSES:
            start_hosted_workflow(conversation_id, turn_id)
        if attachment_ids:
            _file_library().update_links(
                owner_id,
                attachment_ids,
                conversation_id=conversation_id,
                message_id=str((replay_response.get("message") or {}).get("id") or ""),
                turn_id=turn_id,
                profile=str((replay_response.get("hosted_turn") or {}).get("profile") or "default"),
            )
        return replay_response

    route = route_message(
        RouteMessageBody(
            content=message_content,
            mode="auto",
            recent_messages=[
                dict(item)
                for item in payload.recent_messages[-20:]
                if isinstance(item, dict)
            ],
            attachments=route_attachments,
        )
    )
    route, mode, selected_profiles, artifact_required = _hosted_route_parameters(
        route_metadata=route,
        content=message_content,
        requested_mode=str(route.get("mode") or ""),
        requested_profiles=(
            [conversation_profile]
            if str(route.get("mode") or "").strip().lower() == "chat"
            else list(route.get("profiles") or [])
        ),
        requested_artifact=bool(route.get("artifact_required")),
    )
    output_dir = _hosted_turn_output_dir(conversation_id, turn_id).resolve()
    delivery_context = payload.delivery_context.strip()
    if artifact_required:
        delivery_context = "\n".join(
            item
            for item in (
                delivery_context,
                f"Absolute output directory: `{output_dir}`.",
                "Write every generated deliverable to this exact directory, but mention only file names in user-facing text.",
            )
            if item
        )

    with _STATE_LOCK:
        state = load_single_state()
        conversation, _claimed = _owned_conversation_in_state(
            state,
            conversation_id,
            owner_id,
        )
        requests = conversation.get("enqueue_requests")
        if not isinstance(requests, dict):
            requests = {}
            conversation["enqueue_requests"] = requests
        existing_request = requests.get(request_id)
        if isinstance(existing_request, dict):
            if str(existing_request.get("fingerprint") or "") != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="request_id was already used with a different payload",
                )
            response = _enqueued_turn_response(
                conversation,
                existing_request,
                replayed=True,
            )
            accepted = False
        else:
            for file_id in attachment_ids:
                file_record = _file_library().get_file(owner_id, file_id)
                if file_record is None or file_record.get("status") != "available":
                    raise HTTPException(status_code=404, detail="Attachment not found")
            if isinstance((conversation.get("hosted_turns") or {}).get(turn_id), dict):
                raise HTTPException(
                    status_code=409,
                    detail="turn_id already exists outside this request",
                )
            message_id = str(message_source.get("id") or request_id).strip()[:256] or request_id
            messages = conversation.setdefault("messages", [])
            duplicate_message = next(
                (
                    item
                    for item in messages
                    if isinstance(item, dict)
                    and str(item.get("id") or "") == message_id
                ),
                None,
            )
            if duplicate_message is not None:
                raise HTTPException(
                    status_code=409,
                    detail="message id already exists outside this request",
                )
            user_message = _append_message(
                conversation,
                role="user",
                name=str(message_source.get("name") or "user"),
                content=message_content,
                status=str(message_source.get("status") or "completed"),
                kind=str(message_source.get("kind") or "message"),
                meta=dict(message_source.get("meta") or {}),
            )
            user_message["id"] = message_id
            supplied_created_at = message_source.get("created_at")
            if isinstance(supplied_created_at, (int, float)):
                user_message["created_at"] = int(supplied_created_at)
                user_message["updated_at"] = int(
                    message_source.get("updated_at") or supplied_created_at
                )
            _project_native_message(user_message)
            route_message_id = f"route_{hashlib.sha256(request_id.encode('utf-8')).hexdigest()[:20]}"
            route_record = _append_message(
                conversation,
                role="system",
                name=str(route.get("label") or "任务路由"),
                content=str(route.get("reason") or "已完成任务路由。"),
                status="completed",
                kind="route",
                meta={
                    "artifact_required": artifact_required,
                    "confidence": route.get("confidence"),
                    "mode": mode,
                    "profiles": selected_profiles,
                    "source": route.get("source"),
                    "runtime_turn_id": turn_id,
                },
            )
            route_record["id"] = route_message_id
            _project_native_message(route_record)
            hosted = create_hosted_turn_record(
                conversation,
                turn_id=turn_id,
                content=message_content,
                title=str(route.get("title") or ""),
                profiles=selected_profiles,
                artifact_required=artifact_required,
                attachment_context=payload.attachment_context,
                delivery_context=delivery_context,
                user_delivery_context=payload.delivery_context,
                mode=mode,
                route_metadata=route,
                output_dir=str(output_dir),
                attachment_ids=attachment_ids,
            )
            if conversation.get("title") in {"", "新对话", None}:
                conversation["title"] = summarize_task_title(message_content)
            now = int(time.time() * 1000)
            request_record = {
                "request_id": request_id,
                "fingerprint": fingerprint,
                "turn_id": turn_id,
                "message_id": message_id,
                "route_message_id": route_message_id,
                "route": route,
                "created_at": now,
            }
            requests[request_id] = request_record
            if len(requests) > 2000:
                oldest = sorted(
                    requests,
                    key=lambda key: int((requests.get(key) or {}).get("created_at") or 0),
                )[: len(requests) - 2000]
                for key in oldest:
                    requests.pop(key, None)
            save_single_state(state)
            response = _enqueued_turn_response(
                conversation,
                request_record,
                replayed=False,
            )
            accepted = True
    if attachment_ids:
        _file_library().update_links(
            owner_id,
            attachment_ids,
            conversation_id=conversation_id,
            message_id=str((response.get("message") or {}).get("id") or ""),
            turn_id=turn_id,
            profile=str(conversation.get("profile") or "default"),
        )
    start_hosted_workflow(conversation_id, turn_id)
    if accepted:
        _notify_hosted_update()
    return response


@router.post("/single/conversations/{conversation_id}/hosted-turns")
def create_hosted_turn(
    conversation_id: str,
    payload: HostedTurnBody,
    request: Request,
):
    turn_id = payload.turn_id.strip()
    if not turn_id:
        raise HTTPException(status_code=400, detail="turn_id 不能为空")
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="任务内容不能为空")
    owner_id = owner_id_from_request(request)
    with _STATE_LOCK:
        state = load_single_state()
        conversation, claimed = _owned_conversation_in_state(
            state,
            conversation_id,
            owner_id,
        )
        conversation_profile = str(
            conversation.get("profile") or "default"
        ).strip() or "default"
        if claimed:
            save_single_state(state)
    requested_mode = str(
        payload.mode
        or (
            payload.route_metadata.get("mode")
            if isinstance(payload.route_metadata, dict)
            else ""
        )
        or "work"
    ).strip().lower()
    route_metadata, mode, selected_profiles, artifact_required = _hosted_route_parameters(
        route_metadata=payload.route_metadata,
        content=payload.content,
        requested_mode=payload.mode,
        requested_profiles=(
            [conversation_profile]
            if requested_mode == "chat"
            else payload.profiles
        ),
        requested_artifact=payload.artifact_required,
    )
    attachment_ids = list(
        dict.fromkeys(str(item).strip() for item in payload.attachment_ids)
    )
    if len(attachment_ids) > 32 or any(
        not item.startswith("file_") for item in attachment_ids
    ):
        raise HTTPException(status_code=422, detail="Invalid attachment_ids")
    output_dir = _hosted_turn_output_dir(conversation_id, turn_id).resolve()
    delivery_context = payload.delivery_context.strip()
    if artifact_required:
        delivery_context = "\n".join(
            item
            for item in (
                delivery_context,
                f"Absolute output directory: `{output_dir}`.",
                "Write every generated deliverable to this exact directory, but mention only file names in user-facing text.",
            )
            if item
        )
    with _STATE_LOCK:
        state = load_single_state()
        conversation, _claimed = _owned_conversation_in_state(
            state,
            conversation_id,
            owner_id,
        )
        for file_id in attachment_ids:
            file_record = _file_library().get_file(owner_id, file_id)
            if file_record is None or file_record.get("status") != "available":
                raise HTTPException(status_code=404, detail="Attachment not found")
        run = create_hosted_turn_record(
            conversation,
            turn_id=turn_id,
            content=payload.content,
            title=payload.title,
            profiles=selected_profiles,
            artifact_required=artifact_required,
            attachment_context=payload.attachment_context,
            delivery_context=delivery_context,
            user_delivery_context=payload.delivery_context,
            mode=mode,
            route_metadata=route_metadata,
            output_dir=str(output_dir),
            attachment_ids=attachment_ids,
        )
        save_single_state(state)
    if attachment_ids:
        _file_library().update_links(
            owner_id,
            attachment_ids,
            conversation_id=conversation_id,
            turn_id=turn_id,
            profile=str(conversation.get("profile") or "default"),
        )
    start_hosted_workflow(conversation_id, turn_id)
    return {"hosted_turn": _public_hosted_turn(run)}


@router.post(
    "/single/conversations/{conversation_id}/hosted-turns/{turn_id}/cancel"
)
def cancel_hosted_turn(
    conversation_id: str,
    turn_id: str,
    payload: HostedTurnCancellationBody,
    request: Request = None,
):
    _owned_conversation(request, conversation_id)
    try:
        run = request_hosted_turn_cancellation(
            conversation_id,
            turn_id,
            reason=payload.reason,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"hosted_turn": _public_hosted_turn(run)}


def _conversation_deletion_ready(conversation: dict[str, Any]) -> bool:
    """Return true once no local or remote work still needs cancellation state."""

    for run in (conversation.get("hosted_turns") or {}).values():
        if not isinstance(run, dict):
            continue
        if str(run.get("status") or "queued") not in _HOSTED_TERMINAL_STATUSES:
            return False
        if any(
            isinstance(remote, dict)
            and str(remote.get("status") or "queued")
            not in _REMOTE_TERMINAL_STATUSES
            for remote in (run.get("remote_runs") or {}).values()
        ):
            return False
    return True


def _remove_room_index_for_conversation(
    conversation_id: str,
    owner_id: str,
) -> bool:
    """Remove the room alias so a deleted chat cannot be recreated by room ID."""

    room_state = load_state()
    rooms = room_state.get("rooms") or []
    kept: list[dict[str, Any]] = []
    removed = False
    for room in rooms:
        if not isinstance(room, dict):
            continue
        room_owner = str(room.get("owner_id") or "").strip()
        matches_owner = room_owner == owner_id or _legacy_owner_claim_allowed(
            room_owner,
            owner_id,
        )
        if room.get("conversation_id") == conversation_id and matches_owner:
            removed = True
            continue
        kept.append(room)
    if removed:
        room_state["rooms"] = kept
        save_state(room_state)
    return removed


def _finalize_pending_conversation_deletion(conversation_id: str) -> bool:
    """Purge a hidden deletion tombstone after workers no longer need it."""

    runtime_sessions: list[tuple[str, str]] = []
    owner_id = ""
    with _STATE_LOCK:
        state = load_single_state()
        conversation = next(
            (
                item
                for item in state.get("conversations") or []
                if isinstance(item, dict) and item.get("id") == conversation_id
            ),
            None,
        )
        if (
            not isinstance(conversation, dict)
            or not conversation.get("delete_requested")
            or not _conversation_deletion_ready(conversation)
        ):
            return False
        runtime_sessions = [
            (str(profile), str(session_id))
            for profile, session_id in (
                conversation.get("runtime_sessions") or {}
            ).items()
            if str(session_id or "").strip()
        ]
        owner_id = str(conversation.get("owner_id") or LOCAL_OWNER_ID)
        state["conversations"] = [
            item
            for item in state.get("conversations") or []
            if item.get("id") != conversation_id
        ]
        save_single_state(state)
    _remove_room_index_for_conversation(conversation_id, owner_id)
    for profile, session_id in runtime_sessions:
        try:
            _delete_runtime_session(profile, session_id)
        except Exception:
            pass
    shutil.rmtree(conversation_files_root(conversation_id), ignore_errors=True)
    return True


@router.delete("/single/conversations/{conversation_id}")
def delete_single_conversation(
    conversation_id: str,
    request: Request = None,
):
    pending_turn_ids: list[str] = []
    owner_id = owner_id_from_request(request)
    with _STATE_LOCK:
        state = load_single_state()
        conversation, _claimed = _owned_conversation_in_state(
            state,
            conversation_id,
            owner_id,
        )
        _remove_room_index_for_conversation(conversation_id, owner_id)
        now = int(time.time() * 1000)
        conversation.update(
            {
                "delete_requested": True,
                "delete_requested_at": now,
                "updated_at": now,
            }
        )
        for turn_id, run in (conversation.get("hosted_turns") or {}).items():
            if not isinstance(run, dict):
                continue
            has_active_remote = any(
                isinstance(remote, dict)
                and str(remote.get("status") or "queued")
                not in _REMOTE_TERMINAL_STATUSES
                for remote in (run.get("remote_runs") or {}).values()
            )
            if (
                str(run.get("status") or "queued")
                not in _HOSTED_TERMINAL_STATUSES
                or has_active_remote
            ):
                run.update(
                    {
                        "cancel_requested": True,
                        "cancel_reason": "conversation_deleted",
                        "cancel_requested_at": now,
                        "updated_at": now,
                    }
                )
                pending_turn_ids.append(str(turn_id))
        save_single_state(state)
    _notify_hosted_update()
    if not _finalize_pending_conversation_deletion(conversation_id):
        for turn_id in pending_turn_ids:
            start_hosted_workflow(conversation_id, turn_id)
    return {"ok": True}


@router.post("/single/conversations/{conversation_id}/messages")
async def send_single_message(
    conversation_id: str,
    payload: SendSingleMessageBody,
    request: Request = None,
):
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="消息不能为空")

    with _STATE_LOCK:
        state = load_single_state()
        conversation, _claimed = _owned_conversation_in_state(
            state,
            conversation_id,
            owner_id_from_request(request),
        )
        profile = str(conversation.get("profile") or "default")
        _append_message(
            conversation,
            role="user",
            name="用户",
            content=content,
        )
        if conversation.get("title") in {"", "新对话", None}:
            conversation["title"] = summarize_task_title(content)
        prompt = build_single_prompt(conversation, profile, content)
        save_single_state(state)

    try:
        reply = await asyncio.to_thread(run_single_turn, profile, prompt)
        status = "completed"
    except Exception as exc:
        reply = f"执行失败：{str(exc)}"
        status = "failed"

    with _STATE_LOCK:
        state = load_single_state()
        conversation, _claimed = _owned_conversation_in_state(
            state,
            conversation_id,
            owner_id_from_request(request),
        )
        message = _append_message(
            conversation,
            role="assistant",
            name=profile,
            content=reply,
            status=status,
        )
        save_single_state(state)
    return {"ok": status == "completed", "message": message}


@router.get("/rooms")
def get_rooms(request: Request):
    owner_id = owner_id_from_request(request)
    with _STATE_LOCK:
        state = load_state()
        if _claim_legacy_rooms_in_state(state, owner_id):
            save_state(state)
        single_state = load_single_state()
        rooms = [
            room
            for room in state.get("rooms") or []
            if str(room.get("owner_id") or "") == owner_id
            and not _room_maps_to_deleting_conversation(room, single_state)
        ]
        summaries = [
            _room_projection(room, single_state, summary=True)
            for room in rooms
        ]
    return {"rooms": summaries}


@router.post("/rooms")
def create_room(payload: CreateRoomBody, request: Request):
    known = {item["name"] for item in available_profiles()}
    selected = [name for name in payload.profiles if name in known]
    if not selected:
        raise HTTPException(status_code=400, detail="至少选择一个 Hermes Profile")
    owner_id = owner_id_from_request(request)
    with _STATE_LOCK:
        state = load_state()
        single_state = load_single_state()
        room = create_room_record(payload.name, selected, owner_id)
        _room_conversation_in_state(room, single_state, owner_id)
        state["rooms"].insert(0, room)
        save_single_state(single_state)
        save_state(state)
    return {"room": _room_projection(room, single_state, summary=False)}


@router.get("/rooms/{room_id}")
def get_room(room_id: str, request: Request):
    owner_id = owner_id_from_request(request)
    with _STATE_LOCK:
        state = load_state()
        if _claim_legacy_rooms_in_state(
            state,
            owner_id,
            requested_room_id=room_id,
        ):
            save_state(state)
        room = _owned_room_in_state(state, room_id, owner_id)
        single_state = load_single_state()
        if _room_maps_to_deleting_conversation(room, single_state):
            raise HTTPException(status_code=404, detail="Room not found")
        return {
            "room": _room_projection(
                room,
                single_state,
                summary=False,
            )
        }


@router.delete("/rooms/{room_id}")
def delete_room(room_id: str, request: Request):
    owner_id = owner_id_from_request(request)
    with _STATE_LOCK:
        state = load_state()
        _claim_legacy_rooms_in_state(
            state,
            owner_id,
            requested_room_id=room_id,
        )
        room = _owned_room_in_state(state, room_id, owner_id)
        conversation_id = str(room.get("conversation_id") or "")
        if conversation_id:
            try:
                delete_single_conversation(conversation_id, request)
                return {"ok": True}
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
        before = len(state.get("rooms") or [])
        state["rooms"] = [
            room for room in state.get("rooms") or [] if room.get("id") != room_id
        ]
        if len(state["rooms"]) == before:
            raise HTTPException(status_code=404, detail="群聊不存在")
        save_state(state)
    return {"ok": True}


@router.post("/rooms/{room_id}/messages")
def send_message(room_id: str, payload: SendMessageBody, request: Request):
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    owner_id = owner_id_from_request(request)
    request_id = payload.request_id.strip()[:256] or f"room-request-{uuid.uuid4().hex}"
    turn_id = payload.turn_id.strip()[:256] or (
        "room-turn-"
        + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
    )

    with _STATE_LOCK:
        state = load_state()
        _claim_legacy_rooms_in_state(
            state,
            owner_id,
            requested_room_id=room_id,
        )
        room = _owned_room_in_state(state, room_id, owner_id)
        known_profiles = set(room.get("profiles") or [])
        requested = payload.profiles or list(room.get("profiles") or [])
        targets = collaboration_execution_order(
            [name for name in requested if name in known_profiles]
        )
        if not targets:
            raise HTTPException(status_code=400, detail="No executable room member")
        fingerprint = hashlib.sha256(
            json.dumps(
                {"content": content, "profiles": targets, "turn_id": turn_id},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        single_state = load_single_state()
        conversation, _created = _room_conversation_in_state(
            room,
            single_state,
            owner_id,
        )
        room_requests = room.get("hosted_requests")
        if not isinstance(room_requests, dict):
            room_requests = {}
            room["hosted_requests"] = room_requests
        existing = room_requests.get(request_id)
        if not isinstance(existing, dict):
            # The conversation and room indexes live in separate atomic files.
            # If the process stopped after the conversation commit, recover the
            # room-side idempotency record from the hosted turn before appending
            # another user message.
            recovered_run = (conversation.get("hosted_turns") or {}).get(turn_id)
            recovered_request = (
                recovered_run.get("room_request")
                if isinstance(recovered_run, dict)
                else None
            )
            if (
                isinstance(recovered_request, dict)
                and str(recovered_request.get("request_id") or "") == request_id
                and str(recovered_request.get("room_id") or "") == room_id
            ):
                existing = {
                    "fingerprint": str(recovered_request.get("fingerprint") or ""),
                    "message_id": str(recovered_request.get("message_id") or request_id),
                    "turn_id": turn_id,
                    "created_at": int(
                        recovered_request.get("created_at")
                        or recovered_run.get("created_at")
                        or time.time() * 1000
                    ),
                }
                room_requests[request_id] = existing
        if isinstance(existing, dict):
            if str(existing.get("fingerprint") or "") != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="request_id was already used with a different payload",
                )
            existing_turn_id = str(existing.get("turn_id") or turn_id)
            hosted = (conversation.get("hosted_turns") or {}).get(existing_turn_id) or {}
            message = next(
                (
                    item
                    for item in conversation.get("messages") or []
                    if str(item.get("id") or "") == str(existing.get("message_id") or "")
                ),
                {},
            )
            response = {
                "accepted": True,
                "replayed": True,
                "request_id": request_id,
                "turn_id": existing_turn_id,
                "conversation_id": conversation["id"],
                "message": message,
                "messages": [message] if message else [],
                "hosted_turn": _public_hosted_turn(hosted),
            }
            save_single_state(single_state)
            save_state(state)
        else:
            artifact_required = requires_artifact_delivery(content)
            worker_targets = [
                profile.removesuffix("-worker")
                for profile in targets
                if collaboration_role(profile) == "worker"
            ]
            route_metadata, mode, selected_profiles, artifact_required = (
                _hosted_route_parameters(
                    route_metadata={
                        "mode": "work",
                        "profiles": targets,
                        "targets": worker_targets,
                        "artifact_required": artifact_required,
                        "artifact": {
                            "decision": "required" if artifact_required else "none",
                            "types": [],
                            "reason": "Explicit collaboration room request",
                        },
                    },
                    content=content,
                    requested_mode="work",
                    requested_profiles=targets,
                    requested_artifact=artifact_required,
                )
            )
            user_message = _append_message(
                conversation,
                role="user",
                name="User",
                content=content,
                meta={"room_id": room_id, "request_id": request_id},
            )
            user_message["id"] = request_id
            _project_native_message(user_message)
            run = create_hosted_turn_record(
                conversation,
                turn_id=turn_id,
                content=content,
                title=summarize_task_title(content),
                profiles=selected_profiles,
                artifact_required=artifact_required,
                mode=mode,
                route_metadata=route_metadata,
                output_dir=str(
                    _hosted_turn_output_dir(conversation["id"], turn_id).resolve()
                ),
            )
            run["room_request"] = {
                "room_id": room_id,
                "request_id": request_id,
                "fingerprint": fingerprint,
                "message_id": request_id,
                "created_at": int(time.time() * 1000),
            }
            room_requests[request_id] = {
                "fingerprint": fingerprint,
                "message_id": request_id,
                "turn_id": turn_id,
                "created_at": int(time.time() * 1000),
            }
            room["updated_at"] = int(time.time() * 1000)
            save_single_state(single_state)
            save_state(state)
            response = {
                "accepted": True,
                "replayed": False,
                "request_id": request_id,
                "turn_id": turn_id,
                "conversation_id": conversation["id"],
                "message": user_message,
                "messages": [user_message],
                "hosted_turn": _public_hosted_turn(run),
            }
    start_hosted_workflow(str(response["conversation_id"]), str(response["turn_id"]))
    _notify_hosted_update()
    return response


@router.post("/rooms/{room_id}/hosted-turns/{turn_id}/cancel")
def cancel_room_hosted_turn(
    room_id: str,
    turn_id: str,
    payload: HostedTurnCancellationBody,
    request: Request,
):
    owner_id = owner_id_from_request(request)
    with _STATE_LOCK:
        state = load_state()
        if _claim_legacy_rooms_in_state(
            state,
            owner_id,
            requested_room_id=room_id,
        ):
            save_state(state)
        room = _owned_room_in_state(state, room_id, owner_id)
        conversation_id = str(room.get("conversation_id") or "")
        single_state = load_single_state()
        _conversation, claimed = _owned_conversation_in_state(
            single_state,
            conversation_id,
            owner_id,
        )
        if claimed:
            save_single_state(single_state)
    try:
        hosted = request_hosted_turn_cancellation(
            conversation_id,
            turn_id,
            reason=payload.reason,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"hosted_turn": _public_hosted_turn(hosted)}


def _profile_runner_result_error(result: Any) -> str:
    text = _structured_text(result)
    if not text:
        return ""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        if data.get("error"):
            return _structured_text(data.get("error"))
        if data.get("success") is False:
            return _structured_text(data.get("message") or text)
    if re.match(r"^(?:error|failed|exception)\s*[:：]", text, re.IGNORECASE):
        return text
    return ""


def _discover_profile_toolsets(config: dict[str, Any]) -> list[str]:
    """Connect configured MCPs before resolving the hosted agent tool snapshot."""

    from hermes_cli.tools_config import _get_platform_tools
    from tools.mcp_tool import discover_mcp_tools

    discover_mcp_tools()
    return sorted(_get_platform_tools(config, "cli"))


def _profile_event_runner_main() -> int:
    """Child-process entrypoint that emits only structured Hermes JSONL events."""
    real_stdout = sys.stdout
    emit_lock = threading.Lock()

    def emit(event_type: str, payload: Optional[dict[str, Any]] = None) -> None:
        event = {"type": event_type, "payload": payload or {}}
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str)
        with emit_lock:
            real_stdout.write(line + "\n")
            real_stdout.flush()

    try:
        request_payload = json.loads(sys.stdin.read() or "{}")
        prompt = _structured_text(request_payload.get("prompt"))
        requested_session_id = _structured_text(
            request_payload.get("session_id")
        ).strip()
        if not prompt:
            raise ValueError("任务内容为空")

        os.environ["HERMES_YOLO_MODE"] = "1"
        os.environ["HERMES_ACCEPT_HOOKS"] = "1"
        from hermes_cli.config import load_config
        from hermes_cli.env_loader import load_hermes_dotenv
        from hermes_cli.fallback_config import get_fallback_chain
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from hermes_state import SessionDB
        from run_agent import AIAgent

        load_hermes_dotenv(hermes_home=os.environ.get("HERMES_HOME"))
        cfg = load_config()
        # This runner is a fresh process for a hosted chat turn. MCP names in
        # enabled_toolsets are only registry aliases; discovery must connect
        # and register their schemas before AIAgent snapshots its tool list.
        enabled_toolsets = _discover_profile_toolsets(cfg)
        model_cfg = cfg.get("model") or {}
        if isinstance(model_cfg, str):
            model = model_cfg
            provider = None
        else:
            model = str(model_cfg.get("default") or model_cfg.get("model") or "")
            provider = str(model_cfg.get("provider") or "").strip() or None
        runtime = resolve_runtime_provider(
            requested=provider,
            target_model=model or None,
        )
        fallback = get_fallback_chain(cfg)
        tool_started_at: dict[str, float] = {}

        def tool_start(tool_id: str, name: str, args: Any) -> None:
            tool_started_at[str(tool_id)] = time.monotonic()
            emit(
                "tool.start",
                {
                    "tool_id": str(tool_id or ""),
                    "name": str(name or "工具调用"),
                    "args": args,
                    "started_at": int(time.time() * 1000),
                },
            )

        def tool_complete(tool_id: str, name: str, args: Any, result: Any) -> None:
            started = tool_started_at.pop(str(tool_id), time.monotonic())
            error = _profile_runner_result_error(result)
            emit(
                "tool.complete",
                {
                    "tool_id": str(tool_id or ""),
                    "name": str(name or "工具调用"),
                    "args": args,
                    "result_text": _structured_text(result),
                    "error": error,
                    "duration_s": max(0.0, time.monotonic() - started),
                    "ended_at": int(time.time() * 1000),
                },
            )

        def tool_progress(
            event_type: str,
            name: Optional[str] = None,
            preview: Optional[str] = None,
            args: Any = None,
            **kwargs: Any,
        ) -> None:
            if event_type in {"tool.started", "tool.completed", "_thinking"}:
                return
            if event_type == "reasoning.available":
                # This fallback is assistant content, not a distinct thought.
                # Genuine reasoning is emitted by the dedicated callbacks.
                return
            if event_type.startswith("subagent.") or event_type == "subagent_progress":
                normalized_type = (
                    "subagent.progress"
                    if event_type == "subagent_progress"
                    else event_type
                )
                emit(
                    normalized_type,
                    {
                        "name": name or kwargs.get("model") or "子 Agent",
                        "preview": preview or "",
                        "args": args,
                        **kwargs,
                    },
                )
                return
            emit(
                "tool.progress",
                {
                    "name": name or "工具调用",
                    "preview": preview or "",
                    "args": args,
                    "event": event_type,
                },
            )

        session_db = SessionDB()
        resolved_session_id = ""
        conversation_history: list[dict[str, Any]] = []
        if requested_session_id:
            resolved_session_id = str(
                session_db.resolve_resume_session_id(requested_session_id) or ""
            ).strip()
            if resolved_session_id and session_db.get_session(resolved_session_id):
                conversation_history = [
                    message
                    for message in session_db.get_messages_as_conversation(
                        resolved_session_id
                    )
                    if message.get("role") != "session_meta"
                ]
                session_db._conn.execute(
                    "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?",
                    (resolved_session_id,),
                )
                session_db._conn.commit()
            else:
                resolved_session_id = ""
        agent = AIAgent(
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            provider=runtime.get("provider"),
            api_mode=runtime.get("api_mode"),
            model=model,
            max_iterations=45,
            enabled_toolsets=enabled_toolsets,
            quiet_mode=True,
            platform="cli",
            session_db=session_db,
            session_id=resolved_session_id or None,
            credential_pool=runtime.get("credential_pool"),
            fallback_model=fallback or None,
            stream_delta_callback=lambda text: (
                emit("message.delta", {"text": text}) if text is not None else None
            ),
            reasoning_callback=lambda text: emit("reasoning.delta", {"text": text}),
            tool_start_callback=tool_start,
            tool_complete_callback=tool_complete,
            tool_progress_callback=tool_progress,
            tool_gen_callback=lambda name: emit(
                "tool.generating", {"name": str(name or "工具调用")}
            ),
            status_callback=lambda kind, text=None: emit(
                "status.update",
                {"status": str(kind), "text": str(text or kind)},
            ),
        )
        if resolved_session_id:
            agent._session_db_created = True
        agent.suppress_status_output = True
        emit(
            "session.info",
            {
                "session_id": str(getattr(agent, "session_id", "") or ""),
                "model": str(getattr(agent, "model", model) or model),
                "provider": str(
                    getattr(agent, "provider", runtime.get("provider"))
                    or runtime.get("provider")
                    or ""
                ),
            },
        )
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            with redirect_stdout(devnull), redirect_stderr(devnull):
                result = agent.run_conversation(
                    prompt,
                    conversation_history=conversation_history,
                )
        final_response = _structured_text(result.get("final_response"))
        status = "error" if result.get("failed") else "completed"
        emit(
            "message.complete",
            {
                "text": final_response,
                "status": status,
                "session_id": _structured_text(result.get("session_id")),
            },
        )
        return 0 if final_response and status == "completed" else 1
    except BaseException as exc:
        emit(
            "error",
            {
                "message": sanitize_runtime_error(exc),
                "transient": _is_transient_runtime_error(exc),
            },
        )
        return 1


_FILE_LIBRARY: CloudFileLibrary | None = None


def _file_library() -> CloudFileLibrary:
    global _FILE_LIBRARY
    expected_root = (
        Path(get_hermes_home())
        / "collaboration"
        / "account-files"
    )
    if _FILE_LIBRARY is None or _FILE_LIBRARY.root != expected_root:
        _FILE_LIBRARY = CloudFileLibrary(expected_root)
    return _FILE_LIBRARY


def _owned_conversation(
    request: Request,
    conversation_id: str,
) -> tuple[str, dict[str, Any]]:
    """Bind legacy conversations on first file access and enforce ownership."""

    owner_id = owner_id_from_request(request)
    with _STATE_LOCK:
        state = load_single_state()
        conversation, claimed = _owned_conversation_in_state(
            state,
            conversation_id,
            owner_id,
        )
        if claimed:
            save_single_state(state)
    return owner_id, conversation


def _sync_conversation_files(
    owner_id: str,
    conversation: dict[str, Any],
    uploads_dir: Path | None = None,
    outputs_dir: Path | None = None,
    *,
    strict: bool = False,
) -> None:
    conversation_id = str(conversation.get("id") or "").strip()
    if not conversation_id:
        return
    profile = str(conversation.get("profile") or "").strip()
    uploads = uploads_dir or _conversation_file_dir(conversation_id, "uploads")
    outputs = outputs_dir or _conversation_file_dir(conversation_id, "outputs")
    library = _file_library()
    sync_options = {"strict": True} if strict else {}
    library.sync_directory(
        owner_id,
        uploads,
        source="user_upload",
        conversation_id=conversation_id,
        profile=profile,
        origin_prefix=f"conversation:{conversation_id}:uploads",
        **sync_options,
    )
    library.sync_directory(
        owner_id,
        outputs,
        source="model_output",
        conversation_id=conversation_id,
        profile=profile,
        origin_prefix=f"conversation:{conversation_id}:outputs",
        **sync_options,
    )


def _sync_account_conversations(owner_id: str, *, strict: bool = False) -> None:
    """Migrate explicitly bound legacy conversations and discover outputs."""

    with _STATE_LOCK:
        state = load_single_state()
        conversations: list[dict[str, Any]] = []
        changed = False
        for conversation in state.get("conversations") or []:
            existing_owner = str(conversation.get("owner_id") or "").strip()
            if conversation.get("delete_requested"):
                continue
            if existing_owner != owner_id and not _legacy_owner_claim_allowed(
                existing_owner,
                owner_id,
            ):
                continue
            if existing_owner != owner_id:
                conversation["owner_id"] = owner_id
                changed = True
            conversations.append(conversation)
        if changed:
            save_single_state(state)
    for conversation in conversations:
        _sync_conversation_files(owner_id, conversation, strict=strict)


def _account_file_migration_marker(owner_id: str) -> Path:
    owner_hash = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:32]
    return (
        _file_library().root
        / "migrations"
        / f"{_ACCOUNT_FILE_MIGRATION_VERSION}-{owner_hash}.done"
    )


def _migrate_account_conversation_files_once(owner_id: str) -> None:
    """Index pre-library conversation files once for the account that claims them."""

    marker = _account_file_migration_marker(owner_id)
    if marker.is_file():
        return
    with _ACCOUNT_FILE_MIGRATION_LOCK:
        if marker.is_file():
            return
        _sync_account_conversations(owner_id, strict=True)
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                os.chmod(temporary, 0o600)
                handle.write(f"{int(time.time() * 1000)}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, marker)
        finally:
            temporary.unlink(missing_ok=True)


def _library_attachment(record: dict[str, Any]) -> dict[str, Any]:
    file_id = str(record.get("id") or "")
    source = str(record.get("source") or "")
    return {
        key: record.get(key)
        for key in (
            "id",
            "name",
            "sha256",
            "mime_type",
            "extension",
            "file_type",
            "size",
            "source",
            "status",
            "conversation_id",
            "message_id",
            "turn_id",
            "profile",
            "error",
            "created_at",
            "updated_at",
            "available_at",
        )
    } | {
        "bucket": "outputs" if source == "model_output" else "uploads",
        "download_url": f"/api/plugins/collaboration/files/{quote(file_id, safe='')}/download",
    }


def _conversation_library_attachments(
    owner_id: str,
    conversation_id: str,
) -> list[dict[str, Any]]:
    library = _file_library()
    records: list[dict[str, Any]] = []
    offset = 0
    while True:
        page, total = library.list_files(
            owner_id,
            conversation_id=conversation_id,
            limit=200,
            offset=offset,
        )
        records.extend(page)
        offset += len(page)
        if not page or offset >= total:
            break
    return [_library_attachment(record) for record in records]


def _attachment_file_ids(meta: Any) -> list[str]:
    if not isinstance(meta, dict):
        return []
    attachments = meta.get("attachments")
    if not isinstance(attachments, list):
        return []
    return [
        str(attachment.get("id") or "").strip()
        for attachment in attachments
        if isinstance(attachment, dict)
        and str(attachment.get("id") or "").startswith("file_")
    ]


class RegisterArtifactBody(BaseModel):
    relative_path: str = ""
    name: str = ""
    artifact_id: str = ""
    status: str = "available"
    mime_type: str = ""
    message_id: str = ""
    turn_id: str = ""
    profile: str = ""
    error: str = ""


@router.post("/single/conversations/{conversation_id}/artifacts")
def register_conversation_artifact(
    conversation_id: str,
    payload: RegisterArtifactBody,
    request: Request,
):
    """Reserve, publish, or fail a model-created file in ``outputs``."""

    owner_id, conversation = _owned_conversation(request, conversation_id)
    output_root = _conversation_file_dir(conversation_id, "outputs").resolve()
    relative_path = payload.relative_path.strip().replace("\\", "/")
    target = (output_root / relative_path).resolve() if relative_path else None
    if target is not None and not target.is_relative_to(output_root):
        raise HTTPException(status_code=403, detail="Artifact path escapes outputs")
    name = payload.name.strip() or (target.name if target is not None else "")
    profile = payload.profile.strip() or str(conversation.get("profile") or "")
    origin_key = (
        f"conversation:{conversation_id}:outputs:{relative_path}"
        if relative_path
        else ""
    )
    artifact_status = payload.status.strip().lower()
    if artifact_status not in {"uploading", "available", "failed"}:
        raise HTTPException(
            status_code=422,
            detail="Artifact status must be uploading, available, or failed",
        )
    library = _file_library()
    try:
        if artifact_status == "available":
            if target is None or not target.is_file():
                raise HTTPException(status_code=404, detail="Artifact file not found")
            record = library.ingest_file(
                owner_id,
                target,
                name=name,
                source="model_output",
                conversation_id=conversation_id,
                message_id=payload.message_id,
                turn_id=payload.turn_id,
                profile=profile,
                origin_key=origin_key,
                mime_type=payload.mime_type,
                file_id=payload.artifact_id,
                allowed_roots=[output_root],
            )
        else:
            if payload.artifact_id:
                record = library.get_file(owner_id, payload.artifact_id)
                if record is None:
                    raise KeyError(payload.artifact_id)
            else:
                record = library.reserve_file(
                    owner_id,
                    name=name,
                    source="model_output",
                    conversation_id=conversation_id,
                    message_id=payload.message_id,
                    turn_id=payload.turn_id,
                    profile=profile,
                    origin_key=origin_key,
                    mime_type=payload.mime_type,
                )
            record = library.set_status(
                owner_id,
                str(record["id"]),
                artifact_status,
                error=payload.error,
            )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Artifact not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"file": _library_attachment(record)}


@router.post("/files")
async def upload_account_file(request: Request):
    """Upload a durable user-owned file without requiring a chat first."""

    owner_id = owner_id_from_request(request)
    try:
        filename = safe_attachment_name(
            unquote(request.headers.get("x-filename", ""))
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    incoming = _file_library().root / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    temp = incoming / f".{uuid.uuid4().hex}.upload"
    total = 0
    try:
        with temp.open("wb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > _MAX_ATTACHMENT_BYTES:
                    raise HTTPException(status_code=413, detail="文件不能超过 64 MB")
                handle.write(chunk)
        record = _file_library().ingest_file(
            owner_id,
            temp,
            name=filename,
            source="user_upload",
            mime_type=request.headers.get("content-type", ""),
            allowed_roots=[incoming],
        )
    finally:
        temp.unlink(missing_ok=True)
    if record is None:
        raise HTTPException(status_code=500, detail="File upload was not persisted")
    return {"file": _library_attachment(record)}


@router.get("/files")
def list_account_files(
    request: Request,
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    source: str = "",
    file_type: str = "",
    status: str = "",
    limit: int = 100,
    offset: int = 0,
):
    owner_id = owner_id_from_request(request)
    _migrate_account_conversation_files_once(owner_id)
    try:
        start_ms = parse_date_filter(date_from)
        end_ms = parse_date_filter(date_to, end_of_day=True)
        requested_type = file_type or request.query_params.get("type", "")
        records, total = _file_library().list_files(
            owner_id,
            keyword=q,
            date_from=start_ms,
            date_to=end_ms,
            source=source,
            file_type=requested_type,
            status=status,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "files": [_library_attachment(record) for record in records],
        "total": total,
        "limit": max(1, min(limit, 200)),
        "offset": max(0, offset),
    }


@router.get("/files/{file_id}")
def get_account_file(file_id: str, request: Request):
    record = _file_library().get_file(owner_id_from_request(request), file_id)
    if record is None:
        raise HTTPException(status_code=404, detail="File not found")
    return {"file": _library_attachment(record)}


@router.get("/files/{file_id}/download")
def download_account_file(
    file_id: str,
    request: Request,
    preview: bool = False,
):
    try:
        record, path = _file_library().resolve_download(
            owner_id_from_request(request),
            file_id,
        )
    except (KeyError, FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    return FileResponse(
        path=str(path),
        filename=str(record["name"]),
        media_type=str(record["mime_type"]),
        content_disposition_type="inline" if preview else "attachment",
        headers={
            "Cache-Control": "private, no-cache",
            "ETag": f'"{record["sha256"]}"',
        },
    )


@router.delete("/files/{file_id}")
def delete_account_file(file_id: str, request: Request):
    deleted = _file_library().delete_file(
        owner_id_from_request(request),
        file_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="File not found")
    return {"ok": True, "id": file_id}


if __name__ == "__main__" and "--profile-event-runner" in sys.argv:
    raise SystemExit(_profile_event_runner_main())
