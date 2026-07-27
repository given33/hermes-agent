"""Compatibility alias for :mod:`hermes_runtime.colors`."""

from importlib import import_module
import sys

_implementation = import_module("hermes_runtime.colors")
sys.modules[__name__] = _implementation
