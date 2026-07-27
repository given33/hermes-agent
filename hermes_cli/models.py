"""Compatibility alias for the Agent model catalog and transport metadata."""

from __future__ import annotations

import sys

from agent import model_catalog as _implementation

sys.modules[__name__] = _implementation
