"""Compatibility alias for :mod:`hermes_runtime.console_output`."""

from importlib import import_module
import sys

_implementation = import_module("hermes_runtime.console_output")
sys.modules[__name__] = _implementation
