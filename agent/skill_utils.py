"""Compatibility alias for :mod:`hermes_runtime.skill_utils`."""

from importlib import import_module
import sys

_implementation = import_module("hermes_runtime.skill_utils")
sys.modules[__name__] = _implementation
