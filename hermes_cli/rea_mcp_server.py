"""Read-only REA-style artifact analysis MCP provider.

This local adapter is intentionally a safe subset for validation.  It does
not claim to be an upstream REA implementation and it never executes,
extracts, patches, or loads a submitted artifact.  Analysis is bounded to a
configured root and uses the Python standard library so a missing native
reverse-engineering package cannot silently turn into unsafe behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
from typing import Any
import zipfile

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover
    FastMCP = None  # type: ignore[assignment,misc]


PROVIDER_ID = "rea-local-readonly"
PROVIDER_GENERATION = 1
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_READ_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2_000
MAX_RESULTS = 500
_ROOT: Path | None = None


class ReaProviderError(ValueError):
    """A caller supplied an unsafe or unsupported artifact request."""


def configure_root(root: str | os.PathLike[str]) -> Path:
    candidate = Path(root).expanduser().resolve()
    if not candidate.exists() or not candidate.is_dir():
        raise ReaProviderError(f"REA root does not exist: {candidate}")
    global _ROOT
    _ROOT = candidate
    return candidate


def _root() -> Path:
    if _ROOT is None:
        configured = os.environ.get("HERMES_REA_ROOT", "")
        if not configured:
            raise ReaProviderError("HERMES_REA_ROOT is not configured")
        return configure_root(configured)
    return _ROOT


def _safe_path(path: str | os.PathLike[str]) -> tuple[Path, str]:
    root = _root()
    raw = str(path or "").strip()
    if not raw or "\x00" in raw:
        raise ReaProviderError("path is required")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise ReaProviderError("artifact is missing or outside the REA root") from exc
    if not resolved.is_file():
        raise ReaProviderError("artifact must be a regular file")
    if resolved.stat().st_size > MAX_FILE_BYTES:
        raise ReaProviderError(f"artifact exceeds {MAX_FILE_BYTES} byte limit")
    return resolved, relative.as_posix()


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _envelope(tool: str, relative: str, **result: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "provider_id": PROVIDER_ID,
        "provider_generation": PROVIDER_GENERATION,
        "tool": tool,
        "artifact_ref": f"rea://{relative}",
        "read_only": True,
        "result": result,
    }


def _error(tool: str, exc: Exception) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "provider_id": PROVIDER_ID,
        "provider_generation": PROVIDER_GENERATION,
        "tool": tool,
        "read_only": True,
        "ok": False,
        "error": type(exc).__name__,
        "message": str(exc)[:512],
    }


def _read_prefix(path: Path, limit: int = MAX_READ_BYTES) -> bytes:
    with path.open("rb") as handle:
        return handle.read(min(limit, MAX_READ_BYTES))


def _magic(data: bytes) -> str:
    if data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06"):
        return "zip"
    if data[:4] in {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe"}:
        return "macho"
    if data.startswith(b"\x7fELF"):
        return "elf"
    if data.startswith(b"MZ"):
        return "pe"
    if data.startswith(b"\xca\xfe\xba\xbe") or data.startswith(b"\xbe\xba\xfe\xca"):
        return "fat_macho"
    return "unknown"


def inspect_artifact(path: str) -> dict[str, Any]:
    """Return bounded type, size, extension, and digest metadata."""

    try:
        resolved, relative = _safe_path(path)
        prefix = _read_prefix(resolved, 64)
        return _envelope(
            "inspect_artifact",
            relative,
            ok=True,
            extension=resolved.suffix.lower(),
            bytes=resolved.stat().st_size,
            magic=_magic(prefix),
            sha256=_digest(resolved),
        )
    except Exception as exc:
        return _error("inspect_artifact", exc)


def list_archive(path: str, max_entries: int = MAX_ARCHIVE_ENTRIES) -> dict[str, Any]:
    """List archive metadata without extracting any member."""

    try:
        resolved, relative = _safe_path(path)
        if not isinstance(max_entries, int) or not 1 <= max_entries <= MAX_ARCHIVE_ENTRIES:
            raise ReaProviderError(f"max_entries must be between 1 and {MAX_ARCHIVE_ENTRIES}")
        if not zipfile.is_zipfile(resolved):
            raise ReaProviderError("artifact is not a supported zip archive")
        entries = []
        with zipfile.ZipFile(resolved) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise ReaProviderError("archive contains too many entries")
            for info in infos[:max_entries]:
                entries.append(
                    {
                        "name": info.filename[:512],
                        "bytes": int(info.file_size),
                        "compressed_bytes": int(info.compress_size),
                        "is_dir": info.is_dir(),
                    }
                )
        return _envelope(
            "list_archive",
            relative,
            ok=True,
            archive_sha256=_digest(resolved),
            entry_count=len(entries),
            truncated=len(entries) < len(infos),
            entries=entries,
        )
    except Exception as exc:
        return _error("list_archive", exc)


def _strings(data: bytes, max_results: int) -> list[str]:
    found: list[str] = []
    for match in re.findall(rb"[ -~]{4,}", data):
        found.append(match.decode("ascii", errors="replace"))
        if len(found) >= max_results:
            return found
    for match in re.findall(rb"(?:[ -~]\x00){4,}", data):
        text = match.decode("utf-16le", errors="replace").rstrip("\x00")
        if text:
            found.append(text)
        if len(found) >= max_results:
            return found
    return found


def extract_strings(path: str, max_results: int = MAX_RESULTS) -> dict[str, Any]:
    """Extract bounded printable ASCII/UTF-16LE strings from an artifact."""

    try:
        resolved, relative = _safe_path(path)
        if not isinstance(max_results, int) or not 1 <= max_results <= MAX_RESULTS:
            raise ReaProviderError(f"max_results must be between 1 and {MAX_RESULTS}")
        values = _strings(_read_prefix(resolved), max_results)
        return _envelope(
            "extract_strings",
            relative,
            ok=True,
            scanned_bytes=min(resolved.stat().st_size, MAX_READ_BYTES),
            truncated=resolved.stat().st_size > MAX_READ_BYTES,
            strings=values,
        )
    except Exception as exc:
        return _error("extract_strings", exc)


def search_strings(path: str, query: str, max_results: int = MAX_RESULTS) -> dict[str, Any]:
    """Search bounded printable strings case-insensitively."""

    try:
        needle = str(query or "").strip()
        if not needle or len(needle) > 256:
            raise ReaProviderError("query must contain 1 to 256 characters")
        extracted = extract_strings(path, max_results=max_results)
        if not extracted.get("result", {}).get("ok"):
            return extracted
        values = extracted["result"]["strings"]
        matches = [value for value in values if needle.casefold() in value.casefold()]
        return _envelope(
            "search_strings",
            extracted["artifact_ref"].removeprefix("rea://"),
            ok=True,
            query=needle,
            matches=matches[:max_results],
        )
    except Exception as exc:
        return _error("search_strings", exc)


def analyze_macho(path: str) -> dict[str, Any]:
    """Read Mach-O magic and architecture metadata without loading code."""

    try:
        resolved, relative = _safe_path(path)
        data = _read_prefix(resolved, 32)
        magic = _magic(data)
        if magic not in {"macho", "fat_macho"}:
            raise ReaProviderError("artifact does not have a Mach-O magic")
        raw_magic = data[:4]
        endian = "little" if raw_magic in {b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"} else "big"
        word = "64-bit" if raw_magic in {b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe"} else "32-bit"
        cpu = None
        if len(data) >= 8 and magic == "macho":
            cpu = struct.unpack("<I" if endian == "little" else ">I", data[4:8])[0]
        return _envelope(
            "analyze_macho",
            relative,
            ok=True,
            magic=magic,
            endian=endian,
            word_size=word,
            cpu_type=cpu,
            sha256=_digest(resolved),
        )
    except Exception as exc:
        return _error("analyze_macho", exc)


def analyze_bundle(path: str) -> dict[str, Any]:
    """Summarize IPA/zip bundle layout or a bounded text bundle."""

    try:
        resolved, relative = _safe_path(path)
        if zipfile.is_zipfile(resolved):
            with zipfile.ZipFile(resolved) as archive:
                names = [info.filename for info in archive.infolist()[:MAX_ARCHIVE_ENTRIES]]
            payload_apps = sorted(
                {
                    f"Payload/{name.split('/', 2)[1]}/"
                    for name in names
                    if name.startswith("Payload/")
                    and len(name.split("/", 2)) >= 3
                    and name.split("/", 2)[1].endswith(".app")
                }
            )
            has_info = any(name.endswith("Info.plist") for name in names)
            return _envelope(
                "analyze_bundle",
                relative,
                ok=True,
                kind="zip_bundle",
                entry_count=len(names),
                payload_apps=payload_apps[:128],
                has_info_plist=has_info,
                sha256=_digest(resolved),
            )
        data = _read_prefix(resolved)
        return _envelope(
            "analyze_bundle",
            relative,
            ok=True,
            kind="text_or_binary_bundle",
            magic=_magic(data[:64]),
            strings=_strings(data, 32),
            sha256=_digest(resolved),
        )
    except Exception as exc:
        return _error("analyze_bundle", exc)


def _build_server() -> Any:
    if FastMCP is None:
        raise RuntimeError("mcp SDK is required to run the REA provider")
    server = FastMCP(
        PROVIDER_ID,
        instructions=(
            "Read-only REA-style artifact inspection. Never execute, extract, "
            "patch, or load an artifact; all paths are root-confined."
        ),
    )
    server.tool(name="inspect_artifact", description="Inspect safe artifact metadata and digest.")(inspect_artifact)
    server.tool(name="list_archive", description="List archive members without extracting them.")(list_archive)
    server.tool(name="extract_strings", description="Extract bounded printable strings read-only.")(extract_strings)
    server.tool(name="search_strings", description="Search bounded printable strings read-only.")(search_strings)
    server.tool(name="analyze_macho", description="Inspect Mach-O header metadata without execution.")(analyze_macho)
    server.tool(name="analyze_bundle", description="Inspect IPA/zip or text bundle layout without extraction.")(analyze_bundle)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.environ.get("HERMES_REA_ROOT", ""))
    parser.add_argument("--transport", default="stdio", choices=("stdio",))
    args = parser.parse_args(argv)
    configure_root(args.root)
    _build_server().run(transport=args.transport)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
