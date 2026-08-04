"""Compatibility alias for provider-aware model identifier normalization."""

from __future__ import annotations

import sys

from agent import model_normalize as _implementation

sys.modules[__name__] = _implementation
