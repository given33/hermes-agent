"""Compatibility alias for :mod:`hermes_runtime.toolset_validation`."""

from importlib import import_module
import sys

_implementation = import_module("hermes_runtime.toolset_validation")
sys.modules[__name__] = _implementation
