"""Compatibility alias for the runtime credential persistence policy."""

from __future__ import annotations

import sys

from hermes_runtime import credential_persistence as _implementation

sys.modules[__name__] = _implementation
