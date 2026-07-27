"""Compatibility alias for :mod:`hermes_runtime.managed_scope`."""

from importlib import import_module
import sys

_implementation = import_module("hermes_runtime.managed_scope")
sys.modules[__name__] = _implementation
