"""Compatibility alias for normalized Nous account entitlement services."""

from __future__ import annotations

import sys

from agent import nous_account as _implementation

sys.modules[__name__] = _implementation
