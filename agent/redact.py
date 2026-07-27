"""Compatibility alias for :mod:`hermes_runtime.redaction`."""

from importlib import import_module
import sys

_implementation = import_module("hermes_runtime.redaction")
sys.modules[__name__] = _implementation
