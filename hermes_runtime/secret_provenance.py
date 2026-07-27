"""Process-local provenance for credentials loaded from external stores.

The registry is runtime metadata, not a credential store. It records only the
source label for an environment variable so lower layers can explain where a
borrowed secret came from without importing a CLI loader or persisting the
secret value itself.
"""

from __future__ import annotations

from threading import RLock


SECRET_SOURCES: dict[str, str] = {}
_LOCK = RLock()


def get_secret_source(env_var: str) -> str | None:
    """Return the external source label recorded for ``env_var``."""
    with _LOCK:
        return SECRET_SOURCES.get(env_var)


def record_secret_source(env_var: str, source: str) -> None:
    """Record non-secret provenance for one environment variable."""
    name = str(env_var).strip()
    label = str(source).strip()
    if not name or not label:
        return
    with _LOCK:
        SECRET_SOURCES[name] = label


def clear_secret_sources() -> None:
    """Clear process-local provenance, primarily for lifecycle resets/tests."""
    with _LOCK:
        SECRET_SOURCES.clear()
