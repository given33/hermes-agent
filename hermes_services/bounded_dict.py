"""A small insertion-ordered mapping with an eviction ceiling.

Adapted from DeerFlow's harness utility: long-lived registries that cache
per-run/per-generation data need a hard bound so abandoned runs cannot grow
the process indefinitely.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any


class BoundedDict(OrderedDict):
    """Ordered mapping that evicts its oldest key when ``maxsize`` is exceeded."""

    def __init__(self, maxsize: int = 1000, *args: Any, **kwargs: Any) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self.maxsize = int(maxsize)
        super().__init__(*args, **kwargs)

    def __setitem__(self, key: Any, value: Any) -> None:
        if key not in self:
            while len(self) >= self.maxsize:
                self.popitem(last=False)
        super().__setitem__(key, value)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        if key not in self:
            self[key] = default
        return self[key]
