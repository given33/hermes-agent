"""Compatibility alias for the Agent mixture-of-agents configuration model."""

from __future__ import annotations

import sys

from agent import moa_config as _implementation

sys.modules[__name__] = _implementation
