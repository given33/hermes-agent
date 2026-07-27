"""Compatibility alias for :mod:`hermes_runtime.mcp_security`."""

from importlib import import_module
import sys

_implementation = import_module("hermes_runtime.mcp_security")
sys.modules[__name__] = _implementation
