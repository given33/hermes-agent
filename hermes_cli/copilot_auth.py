"""Compatibility alias for GitHub Copilot credential exchange."""

from __future__ import annotations

import sys

from agent import copilot_auth as _implementation

sys.modules[__name__] = _implementation
