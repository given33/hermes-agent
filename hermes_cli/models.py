"""Compatibility alias for the Agent model catalog and transport metadata."""

from __future__ import annotations

from agent import model_catalog as _implementation
import sys

sys.modules[__name__] = _implementation
