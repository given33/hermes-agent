"""Compatibility alias for :mod:`hermes_services.cron_fire`.

Chronos callback verification is a shared transport security boundary, not a
plugin-local HTTP implementation.  Keep the historical path as the exact same
module object for third-party imports and monkeypatch-based integrations.
"""

from importlib import import_module
import sys

_implementation = import_module("hermes_services.cron_fire")
sys.modules[__name__] = _implementation
