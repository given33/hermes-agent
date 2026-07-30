"""Explicit production startup gates for framework-neutral services.

This module is intentionally tiny.  Entry points that can discover or import
dynamic extensions call :func:`bootstrap_trusted_runtime` before doing so.  The
call is explicit at each boundary rather than relying on a later, lazy import
of an individual service implementation.
"""

from __future__ import annotations


def bootstrap_trusted_runtime() -> None:
    """Seal trusted built-in extension state before dynamic code is loaded."""

    from .internal_hooks import bootstrap_internal_hooks

    bootstrap_internal_hooks()
