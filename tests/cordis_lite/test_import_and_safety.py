from __future__ import annotations

import asyncio

from cordis_lite import ComponentCatalog, ComponentSpec, Context, Fiber, FiberState, ServiceBroker
from cordis_lite.coeffects import CoeffectStore
from cordis_lite.loader import ComponentLoader


def test_experiment_package_imports_and_catalog_disposer_is_exact() -> None:
    catalog = ComponentCatalog()
    dispose = catalog.register("component", {"generation": 1})
    assert catalog.get("component") == {"generation": 1}
    dispose()
    dispose()
    assert catalog.get("component") is None


def test_async_context_inverse_is_awaited() -> None:
    calls: list[str] = []

    async def scenario() -> None:
        context = Context()

        async def inverse() -> None:
            calls.append("disposed")

        async def effect():
            yield ("value", inverse)

        await context.effect(effect)
        await context.dispose_async()

    asyncio.run(scenario())
    assert calls == ["disposed"]


def test_broker_round_robin_recovers_after_provider_dispose() -> None:
    broker = ServiceBroker()
    dispose = broker.register("model", "old", object())
    broker.register("model", "new", object())
    assert broker.resolve("model") is not None
    dispose()
    assert broker.resolve("model") is not None


def test_fiber_deactivates_fiber_dependents_before_provider() -> None:
    order: list[str] = []
    store = CoeffectStore(realm={})
    parent = Fiber(
        ComponentSpec("provider", on_deactivate=lambda: order.append("provider")),
        "provider",
        Context(parent=None),
        store,
    )
    child = Fiber(
        ComponentSpec("dependent", on_deactivate=lambda: order.append("dependent")),
        "dependent",
        Context(parent=None),
        store,
    )
    parent.state = child.state = FiberState.ACTIVE
    parent.register_dependency(child)
    asyncio.run(parent.deactivate())
    assert order == ["dependent", "provider"]


def test_fiber_activation_rolls_back_provider_when_apply_fails() -> None:
    store = CoeffectStore(realm={})
    inverses: list[str] = []

    def register(_fiber, _key):
        return lambda: inverses.append("unregistered")

    def fail(_context):
        raise RuntimeError("apply failed")

    fiber = Fiber(ComponentSpec("broken", provide=frozenset({"x"}), apply=fail), "broken", Context(), store)
    try:
        asyncio.run(fiber.activate_async(register))
    except RuntimeError:
        pass
    assert inverses == ["unregistered"]
    assert fiber.state is FiberState.FAILED


def test_loader_rejects_sync_reconcile_inside_loop_and_async_path_executes() -> None:
    async def scenario() -> None:
        loader = ComponentLoader(Context(), CoeffectStore(realm={}), lambda entry: ComponentSpec(entry.id))
        try:
            loader.reconcile({"a": {"url": "a"}})
        except RuntimeError as error:
            assert "reconcile_async" in str(error)
        else:
            raise AssertionError("reconcile must not silently skip inside an event loop")
        assert await loader.reconcile_async({"a": {"url": "a"}}) == ["a"]

    asyncio.run(scenario())


def test_broker_weight_changes_actual_routing_distribution() -> None:
    broker = ServiceBroker()
    light = object()
    heavy = object()
    broker.register("model", "light", light, weight=1)
    broker.register("model", "heavy", heavy, weight=3)
    routed = [broker.resolve("model") for _ in range(40)]
    assert routed.count(heavy) == 30
    assert routed.count(light) == 10
