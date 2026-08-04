"""Compatibility alias for :mod:`hermes_runtime.subprocess_compat`."""

from importlib import import_module
import sys

_implementation = import_module("hermes_runtime.subprocess_compat")
sys.modules[__name__] = _implementation
