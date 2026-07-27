"""Compatibility alias for the Agent fallback-chain policy."""

from __future__ import annotations

import sys

from agent import fallback_config as _implementation

sys.modules[__name__] = _implementation
