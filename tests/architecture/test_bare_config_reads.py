"""Ratchet (a): bare config.yaml reads must never increase.

A "bare read" is a direct ``yaml.safe_load`` whose enclosing function also
builds a ``.../config.yaml`` path — i.e. a config read that bypasses
``hermes_cli.config`` and therefore silently loses defaults merging, managed
scope pins, the (mtime_ns, size) cache, last-known-good recovery, and the
cross-process write lock. The sanctioned replacements are:

- ``hermes_cli.config.load_config()`` / ``load_config_readonly()`` for the
  merged view most callers want,
- ``hermes_cli.config.read_raw_config()`` for the deliberate raw-document
  view (presence checks, "what did the user actually write"),
- ``hermes_cli.config.mutate_config()`` for read→modify→write.

``baselines/bare_config_reads.json`` is the frozen allowlist of remaining
offenders, measured by ``archlint.measure_bare_config_reads()`` (pure AST —
see that docstring for exactly what counts). This test fails when:

- a module NOT in the baseline gains a bare read, or
- a module in the baseline gains MORE bare reads than it had.

Going down is always allowed (and encouraged). After genuinely converting a
caller, tighten the baseline so the improvement can't regress::

    ./.venv-test/Scripts/python.exe tests/architecture/archlint.py --write-baselines

Never regenerate to absorb an increase — that defeats the ratchet.
"""

from __future__ import annotations

import warnings

from tests.architecture import archlint

BASELINE_NAME = "bare_config_reads.json"


def test_no_new_bare_config_reads():
    baseline: dict = archlint.load_baseline(BASELINE_NAME)
    current = archlint.measure_bare_config_reads()

    problems = []

    new_modules = sorted(set(current) - set(baseline))
    if new_modules:
        listing = "\n".join(
            f"  {m}: {current[m]} bare read(s)" for m in new_modules
        )
        problems.append(
            "New module(s) read config.yaml with a bare yaml.safe_load "
            "(bypassing hermes_cli.config — no defaults merge, no managed "
            "scope, no cache, no write lock):\n"
            f"{listing}\n"
            "Use load_config()/load_config_readonly()/read_raw_config()/"
            "mutate_config() instead. Do NOT add these to the baseline."
        )

    regressed = {
        m: (baseline[m], current[m])
        for m in sorted(current)
        if m in baseline and current[m] > baseline[m]
    }
    if regressed:
        listing = "\n".join(
            f"  {m}: baseline {old} -> now {new}"
            for m, (old, new) in regressed.items()
        )
        problems.append(
            "Module(s) gained bare config.yaml reads beyond their frozen "
            f"allowance:\n{listing}"
        )

    assert not problems, "\n\n".join(problems)

    # Improvements are never failures, but a stale baseline leaves headroom
    # for the next regression to hide in — nudge toward tightening it.
    improved = sorted(
        m for m in baseline if current.get(m, 0) < baseline[m]
    )
    if improved:
        warnings.warn(
            "bare-config-read baseline is looser than reality for: "
            + ", ".join(improved)
            + ". Tighten it: ./.venv-test/Scripts/python.exe "
            "tests/architecture/archlint.py --write-baselines",
            stacklevel=1,
        )
