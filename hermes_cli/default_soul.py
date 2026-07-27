"""Compatibility alias for :mod:`hermes_runtime.default_soul`."""

from importlib import import_module
import sys

_implementation = import_module("hermes_runtime.default_soul")
sys.modules[__name__] = _implementation
