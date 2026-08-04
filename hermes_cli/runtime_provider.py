"""Compatibility alias for the Agent runtime provider resolver."""

from __future__ import annotations

import sys

from agent import runtime_provider as _implementation

sys.modules[__name__] = _implementation
