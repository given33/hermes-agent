"""Compatibility route to the gateway's canonical session context.

Hermes 0.20 owns session identity in :mod:`gateway.session_context`. Runtime
services import this module to avoid depending on gateway package layout, but
both paths must expose the same ContextVar objects in a multi-session process.
"""

from gateway.session_context import *  # noqa: F401,F403
