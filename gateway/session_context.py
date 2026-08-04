"""Compatibility alias for :mod:`hermes_runtime.session_context`."""

from importlib import import_module
import sys

_implementation = import_module("hermes_runtime.session_context")
sys.modules[__name__] = _implementation
