"""Cordis-lite: a Python realization of the paper's spatiotemporal composability.

Implements the unified context (revertible effects + reactive coeffects),
the fiber component lifecycle, the declarative loader with transactional
hot module replacement, a service broker, and a Phase-4 orchestration
prototype (hosted-turn fiber trees).
"""

from .effects import Context, EffectStep, EffectNotArmedError
from .coeffects import CoeffectSpec, CoeffectStore
from .component import Fiber, FiberState, ComponentSpec
from .loader import ConfigEntry, ComponentLoader
from .broker import ServiceBroker
from .catalog import ComponentCatalog, catalog_router
from .orchestration import HostedTurnFiberTree

__all__ = [
    "Context", "EffectStep", "EffectNotArmedError",
    "CoeffectSpec", "CoeffectStore",
    "Fiber", "FiberState", "ComponentSpec",
    "ConfigEntry", "ComponentLoader",
    "ServiceBroker",
    "ComponentCatalog", "catalog_router",
    "HostedTurnFiberTree",
]
