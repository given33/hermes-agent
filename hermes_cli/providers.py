"""Compatibility alias for the canonical Agent provider registry."""

from __future__ import annotations

import sys

from agent import provider_registry as _implementation

sys.modules[__name__] = _implementation
