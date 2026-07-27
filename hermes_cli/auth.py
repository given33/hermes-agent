"""Compatibility alias for the provider authentication application service."""

from __future__ import annotations

import sys

from agent import provider_auth as _implementation

sys.modules[__name__] = _implementation
