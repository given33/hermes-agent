"""Ratchet (c): no NEW cross-package imports of underscore-private symbols.

``from other_package.module import _private`` couples the importer to an
implementation detail the owning package never promised to keep — renames
that should be local refactors become cross-package breakage. The baseline
(``baselines/private_symbol_imports.json``) freezes every existing offender
as a ``"<file>::<module>::<symbol>"`` entry; this test fails on any entry
not in that list.

Measured by ``archlint.measure_private_symbol_imports()`` (AST; dunders are
public by convention and excluded; same-package private imports are that
package's own business and excluded). Test code is excluded — tests may
legitimately reach into internals.

The fix for a failure is almost never "add it to the baseline": either use
the public symbol that wraps the private one, or promote the private symbol
to a public name in its owning module (keeping a ``_old = new`` alias if the
owning package still uses the old name internally). Removing entries is
always allowed; afterwards tighten the baseline::

    ./.venv-test/Scripts/python.exe tests/architecture/archlint.py --write-baselines
"""

from __future__ import annotations

import warnings

from tests.architecture import archlint

BASELINE_NAME = "private_symbol_imports.json"


def test_no_new_private_symbol_imports():
    baseline = set(archlint.load_baseline(BASELINE_NAME))
    current = set(archlint.measure_private_symbol_imports())

    new_entries = sorted(current - baseline)
    listing = "\n".join(f"  {e}" for e in new_entries)
    assert not new_entries, (
        "New cross-package private-symbol import(s) (file::module::symbol):\n"
        f"{listing}\n"
        "Import the public wrapper instead, or promote the symbol to a "
        "public name in its owning module. Do NOT add to the baseline."
    )

    removed = sorted(baseline - current)
    if removed:
        warnings.warn(
            f"{len(removed)} private-symbol import(s) in the baseline no "
            "longer exist — tighten it: ./.venv-test/Scripts/python.exe "
            "tests/architecture/archlint.py --write-baselines",
            stacklevel=1,
        )
