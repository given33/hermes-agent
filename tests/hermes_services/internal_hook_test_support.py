"""Test-owned dependency injection for the sealed internal-hook registry.

This module is outside the production package on purpose. Production exposes no
environment switch, reset function, or runtime registration function.
"""

from __future__ import annotations

import hermes_services.internal_hooks as hooks
from hermes_services.internal_hooks import HookPoint, InternalHook


def reset_test_registry() -> None:
    """Install an empty, explicitly unsealed registry for one test process."""

    with hooks._LOCK:
        for entries in hooks._HOOKS.values():
            entries.clear()
        hooks._RUNTIME.clear()
        hooks._HOOK_SLOTS = hooks.BoundedSemaphore(hooks._MAX_HOOK_WORKERS)
        hooks._BOOTSTRAPPED = False
        hooks._SEALED = False


def restore_production_registry() -> None:
    """Restore the code-reviewed built-in registry and seal it."""

    reset_test_registry()
    hooks.bootstrap_internal_hooks()


def register_test_hook(point: HookPoint, hook: InternalHook) -> None:
    """Register through a test-only injection boundary before sealing."""

    with hooks._LOCK:
        if hooks._SEALED:
            raise RuntimeError("internal hook registry is sealed")
        hooks._validate_hook_registration(point, hook)
        entries = hooks._HOOKS[point]
        entries.append(hook)
        entries.sort(key=lambda item: (item.order, item.name))
        hooks._RUNTIME[(point, hook.name)] = hooks._HookRuntimeState()
