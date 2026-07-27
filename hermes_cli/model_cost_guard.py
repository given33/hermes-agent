"""Compatibility alias for the Agent model cost guardrail."""

from __future__ import annotations

import sys

from agent import model_cost_guard as _implementation

sys.modules[__name__] = _implementation
