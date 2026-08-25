"""Read-only visual evidence MCP provider for Hermes validation.

This is a local, dependency-light sidecar.  It intentionally does not claim
to be a general vision model: it provides deterministic image inspection,
pixel probing, pixel diffs, and bounded region detection.  OCR is exposed only
when the optional pytesseract dependency is installed.  Every operation is
root-confined and returns provenance without leaking an absolute host path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from PIL import Image

try:
    import numpy as np
except ImportError:  # pragma: no cover - the validation environment installs it
    np = None  # type: ignore[assignment]

try:
    from mcp.server import MCPServer as FastMCP
except ImportError:  # pragma: no cover - MCP 1.x compatibility
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:  # pragma: no cover - exercised by the CLI doctor instead
        FastMCP = None  # type: ignore[assignment,misc]


PROVIDER_ID = "hermes-visual-evidence"
PROVIDER_GENERATION = 1
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 24_000_000
MAX_REGIONS = 256
_ROOT: Path | None = None


class VisualProviderError(ValueError):
    """A caller supplied an invalid or out-of-bound visual artifact."""


def configure_root(root: str | os.PathLike[str]) -> Path:
    """Set and return the absolute root used by the provider."""

    candidate = Path(root).expanduser().resolve()
    if not candidate.exists() or not candidate.is_dir():
        raise VisualProviderError(f"visual root does not exist: {candidate}")
    global _ROOT
    _ROOT = candidate
    return candidate


def _root() -> Path:
    if _ROOT is None:
        configured = os.environ.get("HERMES_VISUAL_ROOT", "")
        if not configured:
            raise VisualProviderError("HERMES_VISUAL_ROOT is not configured")
        return configure_root(configured)
    return _ROOT


def _safe_path(path: str | os.PathLike[str]) -> tuple[Path, str]:
    root = _root()
    raw = str(path or "").strip()
    if not raw or "\x00" in raw:
        raise VisualProviderError("path is required")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise VisualProviderError("artifact is missing or outside the visual root") from exc
    if not resolved.is_file():
        raise VisualProviderError("artifact must be a regular file")
    size = resolved.stat().st_size
    if size > MAX_FILE_BYTES:
        raise VisualProviderError(f"artifact exceeds {MAX_FILE_BYTES} byte limit")
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
        "artifact_ref": f"visual://{relative}",
        "read_only": True,
        "result": result,
    }


def _load_image(path: str) -> tuple[Path, str, Image.Image]:
    resolved, relative = _safe_path(path)
    try:
        image = Image.open(resolved)
        image.load()
    except Exception as exc:
        raise VisualProviderError("artifact is not a readable image") from exc
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        raise VisualProviderError("image dimensions exceed the provider limit")
    return resolved, relative, image


def _json_error(tool: str, error: Exception) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "provider_id": PROVIDER_ID,
        "provider_generation": PROVIDER_GENERATION,
        "tool": tool,
        "read_only": True,
        "ok": False,
        "error": type(error).__name__,
        "message": str(error)[:512],
    }


def inspect_image(path: str) -> dict[str, Any]:
    """Inspect image metadata and digest without returning image bytes."""

    try:
        resolved, relative = _safe_path(path)
        with Image.open(resolved) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                raise VisualProviderError("image dimensions exceed the provider limit")
            result = {
                "ok": True,
                "format": str(image.format or "unknown"),
                "mode": image.mode,
                "width": width,
                "height": height,
                "bytes": resolved.stat().st_size,
                "sha256": _digest(resolved),
            }
        return _envelope("inspect_image", relative, **result)
    except Exception as exc:
        return _json_error("inspect_image", exc)


def pixel_probe(path: str, x: int, y: int) -> dict[str, Any]:
    """Return one bounded pixel and normalized coordinates."""

    try:
        resolved, relative, image = _load_image(path)
        if not isinstance(x, int) or not isinstance(y, int):
            raise VisualProviderError("x and y must be integers")
        if not (0 <= x < image.width and 0 <= y < image.height):
            raise VisualProviderError("pixel coordinate is outside the image")
        pixel = image.convert("RGBA").getpixel((x, y))
        return _envelope(
            "pixel_probe",
            relative,
            ok=True,
            x=x,
            y=y,
            normalized_x=round(x / max(image.width - 1, 1), 6),
            normalized_y=round(y / max(image.height - 1, 1), 6),
            rgba=list(pixel),
            sha256=_digest(resolved),
        )
    except Exception as exc:
        return _json_error("pixel_probe", exc)


def pixel_diff(before: str, after: str, threshold: int = 0) -> dict[str, Any]:
    """Compare two images and return a bounded geometric diff summary."""

    try:
        before_path, before_ref, before_image = _load_image(before)
        after_path, after_ref, after_image = _load_image(after)
        if before_image.size != after_image.size:
            raise VisualProviderError("pixel diff requires equal image dimensions")
        if not isinstance(threshold, int) or not 0 <= threshold <= 255:
            raise VisualProviderError("threshold must be an integer from 0 to 255")
        if np is None:
            raise VisualProviderError("numpy is required for pixel diff")
        left = np.asarray(before_image.convert("RGBA"), dtype=np.int16)
        right = np.asarray(after_image.convert("RGBA"), dtype=np.int16)
        delta = np.max(np.abs(left - right), axis=2)
        changed = delta > threshold
        ys, xs = np.where(changed)
        bbox = None if len(xs) == 0 else [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
        diff_digest = hashlib.sha256(delta.astype(np.uint8).tobytes()).hexdigest()
        return {
            "schema_version": "1.0",
            "provider_id": PROVIDER_ID,
            "provider_generation": PROVIDER_GENERATION,
            "tool": "pixel_diff",
            "artifact_ref": f"visual://{before_ref}",
            "read_only": True,
            "ok": True,
            "result": {
                "before_ref": f"visual://{before_ref}",
                "after_ref": f"visual://{after_ref}",
                "before_sha256": _digest(before_path),
                "after_sha256": _digest(after_path),
                "width": before_image.width,
                "height": before_image.height,
                "threshold": threshold,
                "changed_pixels": int(changed.sum()),
                "changed_ratio": round(float(changed.mean()), 8),
                "max_delta": int(delta.max()) if delta.size else 0,
                "bbox": bbox,
                "diff_sha256": diff_digest,
            },
        }
    except Exception as exc:
        return _json_error("pixel_diff", exc)


def regions(path: str, threshold: int = 16) -> dict[str, Any]:
    """Find a bounded foreground region relative to the top-left background."""

    try:
        _resolved, relative, image = _load_image(path)
        if not isinstance(threshold, int) or not 0 <= threshold <= 255:
            raise VisualProviderError("threshold must be an integer from 0 to 255")
        if np is None:
            raise VisualProviderError("numpy is required for region detection")
        rgba = np.asarray(image.convert("RGBA"), dtype=np.int16)
        background = rgba[0, 0]
        mask = np.max(np.abs(rgba - background), axis=2) > threshold
        ys, xs = np.where(mask)
        found = []
        if len(xs):
            found.append(
                {
                    "id": "foreground-0",
                    "bbox": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())],
                    "area": int(mask.sum()),
                    "confidence": 1.0,
                }
            )
        return _envelope(
            "regions",
            relative,
            ok=True,
            threshold=threshold,
            background_rgba=background.tolist(),
            regions=found[:MAX_REGIONS],
        )
    except Exception as exc:
        return _json_error("regions", exc)


def ocr(path: str, language: str = "eng") -> dict[str, Any]:
    """Run optional local OCR, or return an explicit unavailable result."""

    try:
        _resolved, relative, image = _load_image(path)
        try:
            import pytesseract
        except ImportError:
            return _envelope(
                "ocr",
                relative,
                ok=False,
                status="unavailable",
                reason="optional pytesseract dependency is not installed",
            )
        text = pytesseract.image_to_string(image, lang=language)[:16_384]
        return _envelope("ocr", relative, ok=True, status="complete", text=text)
    except Exception as exc:
        return _json_error("ocr", exc)


def _build_server() -> Any:
    if FastMCP is None:
        raise RuntimeError("mcp SDK is required to run the visual provider")
    server = FastMCP(
        PROVIDER_ID,
        instructions=(
            "Read-only visual evidence provider. Paths are restricted to the "
            "configured validation root; use inspect_image before pixel tools."
        ),
    )
    server.tool(name="inspect_image", description="Read image metadata and SHA-256 digest.")(inspect_image)
    server.tool(name="pixel_probe", description="Read one pixel using image coordinates.")(pixel_probe)
    server.tool(name="pixel_diff", description="Compute a deterministic pixel diff between two images.")(pixel_diff)
    server.tool(name="regions", description="Detect a bounded foreground region against the image background.")(regions)
    server.tool(name="ocr", description="Run optional local OCR and report unavailable explicitly when absent.")(ocr)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.environ.get("HERMES_VISUAL_ROOT", ""))
    parser.add_argument("--transport", default="stdio", choices=("stdio",))
    args = parser.parse_args(argv)
    configure_root(args.root)
    _build_server().run(transport=args.transport)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
