"""Compatibility alias for :mod:`hermes_runtime.secret_scope`."""

from importlib import import_module
import sys

_implementation = import_module("hermes_runtime.secret_scope")
sys.modules[__name__] = _implementation
