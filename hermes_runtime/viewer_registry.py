"""Client-neutral viewer registry for file, diff, terminal, and artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping


@dataclass(frozen=True)
class Viewer:
    viewer_id: str
    kinds: tuple[str, ...]
    version: str = "1"
    replayable: bool = True


class ViewerRegistry:
    def __init__(self) -> None:
        self._viewers: dict[str, Viewer] = {}

    def register(self, viewer_id: str, *, kinds: tuple[str, ...], version: str = "1", replayable: bool = True) -> Viewer:
        normalized = str(viewer_id or "").strip()
        if not normalized or not kinds:
            raise ValueError("viewer_id and kinds are required")
        viewer = Viewer(normalized, tuple(dict.fromkeys(str(kind).strip() for kind in kinds if str(kind).strip())), str(version), bool(replayable))
        self._viewers[normalized] = viewer
        return viewer

    def resolve(self, kind: str) -> list[Viewer]:
        normalized = str(kind or "").strip()
        return [viewer for viewer in self._viewers.values() if normalized in viewer.kinds]

    def snapshot(self) -> list[dict[str, object]]:
        return [{"viewer_id": item.viewer_id, "kinds": list(item.kinds), "version": item.version, "replayable": item.replayable} for item in sorted(self._viewers.values(), key=lambda item: item.viewer_id)]


DEFAULT_VIEWERS = ViewerRegistry()
for _viewer_id, _kinds in (("file", ("file",)), ("diff", ("diff", "patch")), ("terminal", ("terminal", "log")), ("artifact", ("artifact", "image", "evidence"))):
    DEFAULT_VIEWERS.register(_viewer_id, kinds=_kinds)
