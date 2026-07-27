"""Compatibility alias for :mod:`hermes_runtime.runtime_cwd`."""

from importlib import import_module
import sys

_implementation = import_module("hermes_runtime.runtime_cwd")
sys.modules[__name__] = _implementation
