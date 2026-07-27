"""Compatibility alias for :mod:`hermes_runtime.timeouts`."""

from importlib import import_module
import sys

_implementation = import_module("hermes_runtime.timeouts")
sys.modules[__name__] = _implementation
