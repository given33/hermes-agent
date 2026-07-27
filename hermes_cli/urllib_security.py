"""Compatibility alias for :mod:`hermes_runtime.urllib_security`."""

from importlib import import_module
import sys

_implementation = import_module("hermes_runtime.urllib_security")
sys.modules[__name__] = _implementation
