"""Ratchet (b): forbidden dependency directions must never densify.

The six legacy packages still contain a historical mesh. New foundation
packages now provide the downward migration path described in
``docs/architecture/layering.md``. This test freezes upward and same-layer
edges. Downward edges may grow when a complete behavior moves out of an entry
point into its owning layer; treating that improvement as a regression would
lock the original mesh in place. The separate hard-layering test keeps every
upward edge from the foundation packages at zero.

1. **Cross-package import edges** — for each directed pair ``a->b``, the
   number of forbidden import statements in package ``a`` that target package
   ``b`` must not exceed the frozen count in
   ``baselines/dependency_direction.json``, and no new forbidden direction may
   appear. New downward imports are allowed because they replace upward debt.
2. **Deferred (function-body) imports** — per source package, the count of
   cross-package imports hidden inside function bodies must not exceed the
   baseline. A deferred import is how a new cycle sneaks in without an
   ImportError, which is exactly why its count is ratcheted.

Measured by ``archlint.measure_dependency_direction()`` (AST, relative
imports excluded as intra-package by construction). Reducing an edge is
always allowed; after a genuine reduction, tighten the baseline::

    ./.venv-test/Scripts/python.exe tests/architecture/archlint.py --write-baselines

Never regenerate to absorb an increase in a legacy edge.
"""

from __future__ import annotations

import warnings

from tests.architecture import archlint

BASELINE_NAME = "dependency_direction.json"
PACKAGE_LAYERS = {
    "hermes_runtime": 1,
    "hermes_services": 2,
    "agent": 3,
    "tools": 4,
    "gateway": 5,
    "plugins": 5,
    "hermes_cli": 6,
    "tui_gateway": 6,
}


def _is_forbidden_direction(edge: str) -> bool:
    """Return whether an edge points upward or couples peer adapters."""

    source, target = edge.split("->", 1)
    return PACKAGE_LAYERS[source] <= PACKAGE_LAYERS[target]


def _current_and_baseline():
    baseline = archlint.load_baseline(BASELINE_NAME)
    current = archlint.measure_dependency_direction()
    return current, baseline


def test_no_new_or_heavier_cross_package_edges():
    current, baseline = _current_and_baseline()
    cur_edges: dict = current["cross_package_import_statements"]
    base_edges: dict = baseline["cross_package_import_statements"]

    problems = []

    new_edges = sorted(
        edge
        for edge in set(cur_edges) - set(base_edges)
        if _is_forbidden_direction(edge)
    )
    if new_edges:
        listing = "\n".join(
            f"  {e}: {cur_edges[e]} import statement(s)" for e in new_edges
        )
        problems.append(
            "New cross-package dependency direction(s) introduced — these "
            "pairs had ZERO imports at baseline and must stay that way "
            "unless docs/architecture/layering.md is deliberately revised:\n"
            f"{listing}"
        )

    heavier = {
        e: (base_edges[e], cur_edges[e])
        for e in sorted(cur_edges)
        if (
            e in base_edges
            and _is_forbidden_direction(e)
            and cur_edges[e] > base_edges[e]
        )
    }
    if heavier:
        listing = "\n".join(
            f"  {e}: baseline {old} -> now {new}"
            for e, (old, new) in heavier.items()
        )
        problems.append(
            "Cross-package edge(s) got heavier. Prefer moving shared code "
            "into the lower-level package (or a new leaf module) over adding "
            f"another import across the boundary:\n{listing}"
        )

    assert not problems, "\n\n".join(problems)

    improved = sorted(
        e
        for e in base_edges
        if _is_forbidden_direction(e) and cur_edges.get(e, 0) < base_edges[e]
    )
    if improved:
        warnings.warn(
            "dependency-direction baseline is looser than reality for "
            "edge(s): " + ", ".join(improved)
            + ". Tighten it: ./.venv-test/Scripts/python.exe "
            "tests/architecture/archlint.py --write-baselines",
            stacklevel=1,
        )


def test_no_new_deferred_cross_package_imports():
    current, baseline = _current_and_baseline()
    cur_def: dict = current["deferred_cross_package_imports"]
    base_def: dict = baseline["deferred_cross_package_imports"]

    # A package absent from the baseline map has a zero allowance.
    regressed = {
        pkg: (base_def.get(pkg, 0), n)
        for pkg, n in sorted(cur_def.items())
        if n > base_def.get(pkg, 0)
    }
    listing = "\n".join(
        f"  {pkg}: baseline {old} -> now {new}"
        for pkg, (old, new) in regressed.items()
    )
    assert not regressed, (
        "Function-body (deferred) cross-package imports increased:\n"
        f"{listing}\n"
        "A deferred import is the standard way an import cycle hides — the "
        "module imports fine until the function first runs. If the import "
        "can live at module top level without an ImportError, put it there "
        "(the edge ratchet still applies); if it cannot, you are adding a "
        "cycle — restructure instead."
    )

    improved = sorted(
        pkg for pkg in base_def if cur_def.get(pkg, 0) < base_def[pkg]
    )
    if improved:
        warnings.warn(
            "deferred-import baseline is looser than reality for package(s): "
            + ", ".join(improved)
            + ". Tighten it: ./.venv-test/Scripts/python.exe "
            "tests/architecture/archlint.py --write-baselines",
            stacklevel=1,
        )
