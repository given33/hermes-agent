"""Compatibility alias for :mod:`hermes_runtime.secret_prompt`."""

from importlib import import_module
import sys

_implementation = import_module("hermes_runtime.secret_prompt")
sys.modules[__name__] = _implementation
