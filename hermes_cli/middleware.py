"""Compatibility alias for :mod:`hermes_services.middleware`."""

from __future__ import annotations

import sys

from hermes_services import middleware as _implementation

sys.modules[__name__] = _implementation
