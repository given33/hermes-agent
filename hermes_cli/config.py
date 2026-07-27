"""Compatibility alias for :mod:`hermes_runtime.config`.

The configuration authority moved out of the CLI package.  This module keeps
the historical import path bound to the exact same module object so existing
plugins and monkeypatch-based integrations continue to observe one cache,
one write lock, and one set of functions.
"""

from importlib import import_module
import sys

_implementation = import_module("hermes_runtime.config")
sys.modules[__name__] = _implementation
