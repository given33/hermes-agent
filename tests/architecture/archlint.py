"""Shared AST-based measurement helpers for the architecture ratchet tests.

Everything in here is *measurement*, not policy: it parses first-party
source with :mod:`ast` (never regex) and returns plain dicts/sets that the
ratchet tests compare against checked-in baselines under ``baselines/``.

Design constraints (why it looks the way it does):

- **AST only.** Regex over source text miscounts imports in strings and
  docstrings; several modules here embed example code in docstrings.
- **One parse per file per session.** The repo has a few very large modules
  (cli.py, gateway/run.py are each >15k lines); ``parse_file`` caches the
  tree so the four ratchets share one parse.
- **Deterministic output.** All maps are returned with sorted keys so the
  baselines are stable, diffable JSON.

Regenerating baselines after an intentional improvement::

    ./.venv-test/Scripts/python.exe tests/architecture/archlint.py --write-baselines

(Never regenerate to hide a regression — the ratchets exist so the numbers
in ``baselines/`` can only go down.)

Run without flags to print the current measurements without writing.
"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_DIR = Path(__file__).resolve().parent / "baselines"

# The packages whose cross-imports form the dependency mesh.  The two runtime
# foundations are included so their zero up-edges are enforced, not merely
# described in architecture prose.
RATCHET_PACKAGES: Tuple[str, ...] = (
    "hermes_runtime",
    "hermes_services",
    "agent",
    "tools",
    "gateway",
    "hermes_cli",
    "plugins",
    "tui_gateway",
)

# Directories never scanned (vendored/venv/build output — not first-party
# source, and some are enormous).
EXCLUDED_DIR_NAMES = {
    "__pycache__",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".venv-test",
    "venv",
    "node_modules",
    "site-packages",
    "dist",
    "build",
}

# Top-level repo directories that are not Python-source scan targets for the
# whole-repo scans (bare-config-read, wired-security). Anything containing
# only docs/assets/examples — skipping them keeps the scan fast.
NON_SOURCE_TOP_DIRS = {
    "assets",
    "docs",
    "infographic",
    "locales",
    "website",
    "web",
    "ui-tui",
    "contributors",
    "datagen-config-examples",
    "nix",
    "packaging",
    # Bundled skill/plugin content is partially user-authored and follows its
    # own conventions (same carve-out pyproject.toml makes for ruff PLW1514);
    # a "config.yaml" mentioned there is usually the skill's own file, not
    # ~/.hermes/config.yaml, so scanning it would only produce false ratchet
    # entries.
    "skills",
    "optional-skills",
    "optional-mcps",
}


def _iter_py_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIR_NAMES]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def iter_package_files(packages: Iterable[str] = RATCHET_PACKAGES) -> Iterator[Path]:
    """All .py files under the ratchet packages."""
    for pkg in packages:
        pkg_root = REPO_ROOT / pkg
        if pkg_root.is_dir():
            yield from _iter_py_files(pkg_root)


def iter_repo_source_files(include_tests: bool = False) -> Iterator[Path]:
    """All first-party .py files in the repo (packages + top-level modules).

    ``include_tests=False`` (the default for every ratchet) skips ``tests/``
    and ``tests-js/`` — test code may legitimately exercise private symbols
    and raw YAML fixtures.
    """
    for entry in sorted(REPO_ROOT.iterdir()):
        name = entry.name
        if name in EXCLUDED_DIR_NAMES or name in NON_SOURCE_TOP_DIRS:
            continue
        if not include_tests and name in {"tests", "tests-js"}:
            continue
        if entry.is_file() and name.endswith(".py"):
            yield entry
        elif entry.is_dir():
            yield from _iter_py_files(entry)


def rel(path: Path) -> str:
    """Repo-relative POSIX path — the stable key format used in baselines."""
    return path.relative_to(REPO_ROOT).as_posix()


# ── Parse cache ──────────────────────────────────────────────────────────────

_AST_CACHE: Dict[str, Optional[ast.Module]] = {}


def parse_file(path: Path) -> Optional[ast.Module]:
    """Parse ``path`` (cached). Returns None for unparseable files.

    Unparseable first-party files are someone else's build breakage — the
    ratchets skip them rather than aborting the whole measurement.
    """
    key = str(path)
    if key not in _AST_CACHE:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree: Optional[ast.Module] = ast.parse(source, filename=key)
        except SyntaxError:
            tree = None
        _AST_CACHE[key] = tree
    return _AST_CACHE[key]


def annotate_parents(tree: ast.Module) -> None:
    """Attach ``.arch_parent`` to every node (idempotent per tree)."""
    if getattr(tree, "_arch_parents_done", False):
        return
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.arch_parent = node  # type: ignore[attr-defined]
    tree._arch_parents_done = True  # type: ignore[attr-defined]


def enclosing_function(node: ast.AST) -> Optional[ast.AST]:
    """Innermost enclosing FunctionDef/AsyncFunctionDef, or None (module level).

    Lambdas are deliberately NOT scope boundaries here: patterns like
    ``_cached_read(path, cache, lambda f: yaml.safe_load(f))`` should be
    attributed to the named function that owns the lambda.
    """
    cur = getattr(node, "arch_parent", None)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = getattr(cur, "arch_parent", None)
    return None


def top_package(module_name: str) -> str:
    return module_name.split(".", 1)[0]


def source_top(path: Path) -> str:
    """Top-level namespace a file belongs to: package dir name or module stem."""
    parts = path.relative_to(REPO_ROOT).parts
    if len(parts) == 1:
        return Path(parts[0]).stem  # top-level module, e.g. utils.py -> "utils"
    return parts[0]


def first_party_top_names() -> Set[str]:
    """Top-level names that resolve to first-party code (packages + modules)."""
    names: Set[str] = set()
    for entry in REPO_ROOT.iterdir():
        if entry.is_file() and entry.name.endswith(".py"):
            names.add(entry.stem)
        elif (
            entry.is_dir()
            and entry.name not in EXCLUDED_DIR_NAMES
            and entry.name not in NON_SOURCE_TOP_DIRS
            and (entry / "__init__.py").exists()
        ):
            names.add(entry.name)
    return names


# ── (a) Bare config.yaml reads ───────────────────────────────────────────────


def _yaml_safe_load_calls(tree: ast.Module) -> List[ast.Call]:
    """All calls that resolve to yaml.safe_load in this module.

    Handles ``import yaml``, ``import yaml as X``, and
    ``from yaml import safe_load [as Y]``. Does NOT count
    ``utils.fast_safe_load`` / ``hermes_cli.config`` wrappers — those ARE the
    sanctioned chokepoints.
    """
    module_aliases: Set[str] = set()   # names bound to the yaml module
    direct_names: Set[str] = set()     # names bound to yaml.safe_load itself
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "yaml" or alias.name.startswith("yaml."):
                    module_aliases.add(alias.asname or top_package(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module == "yaml":
                for alias in node.names:
                    if alias.name == "safe_load":
                        direct_names.add(alias.asname or alias.name)

    calls: List[ast.Call] = []
    if not module_aliases and not direct_names:
        return calls
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "safe_load"
            and isinstance(func.value, ast.Name)
            and func.value.id in module_aliases
        ):
            calls.append(node)
        elif isinstance(func, ast.Name) and func.id in direct_names:
            calls.append(node)
    return calls


def _scope_mentions_config_yaml(scope: ast.AST) -> bool:
    for node in ast.walk(scope):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "config.yaml" in node.value:
                return True
    return False


def measure_bare_config_reads() -> Dict[str, int]:
    """Per-module count of yaml.safe_load calls whose enclosing scope also
    references a ``config.yaml`` path constant.

    This is the AST version of the audit's "bare config read" finding: a
    direct ``yaml.safe_load`` where the surrounding function builds a
    ``.../config.yaml`` path — i.e. a config read that bypasses
    ``hermes_cli.config`` (defaults merge, managed scope, caching,
    last-known-good, cross-process lock). Scope attribution is the innermost
    named function (module scope for top-level calls), so a module that
    merely *mentions* config.yaml elsewhere is not flagged for unrelated
    safe_load calls.
    """
    counts: Dict[str, int] = {}
    for path in iter_repo_source_files(include_tests=False):
        tree = parse_file(path)
        if tree is None:
            continue
        calls = _yaml_safe_load_calls(tree)
        if not calls:
            continue
        annotate_parents(tree)
        n = 0
        for call in calls:
            scope = enclosing_function(call) or tree
            if _scope_mentions_config_yaml(scope):
                n += 1
        if n:
            counts[rel(path)] = n
    return dict(sorted(counts.items()))


# ── (b) Dependency-direction edges ───────────────────────────────────────────


def measure_dependency_direction() -> Dict[str, Dict[str, int]]:
    """Cross-package import-statement counts among RATCHET_PACKAGES.

    Returns::

        {
          "cross_package_import_statements": {"hermes_cli->agent": N, ...},
          "deferred_cross_package_imports": {"gateway": N, ...},
        }

    - An *import statement* is one Import/ImportFrom node; a single node
      naming two cross-package targets counts once per target package.
    - *Deferred* means the statement's innermost enclosing scope is a
      function body — the repo's standard trick for hiding an import cycle,
      which is exactly why its count must not grow.
    - Relative imports are intra-package by construction and never counted.
    """
    edge_counts: Dict[str, int] = {}
    deferred_counts: Dict[str, int] = {pkg: 0 for pkg in RATCHET_PACKAGES}
    pkg_set = set(RATCHET_PACKAGES)

    for path in iter_package_files():
        src_pkg = source_top(path)
        tree = parse_file(path)
        if tree is None:
            continue
        annotate_parents(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = {top_package(a.name) for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    continue  # relative import — intra-package
                targets = {top_package(node.module)}
            else:
                continue
            cross = {t for t in targets if t in pkg_set and t != src_pkg}
            if not cross:
                continue
            deferred = enclosing_function(node) is not None
            for target in cross:
                key = f"{src_pkg}->{target}"
                edge_counts[key] = edge_counts.get(key, 0) + 1
                if deferred:
                    deferred_counts[src_pkg] += 1

    return {
        "cross_package_import_statements": dict(sorted(edge_counts.items())),
        "deferred_cross_package_imports": dict(sorted(deferred_counts.items())),
    }


def deferred_imports_by_file(top_n: int = 10) -> List[Tuple[str, int]]:
    """Report helper (not a ratchet): heaviest function-body importers.

    Counts EVERY first-party deferred import (any target in the repo's
    top-level namespace, including utils/cli/hermes_constants), matching how
    the audit characterized gateway/run.py etc. — those files defer far more
    than just the six ratchet packages.
    """
    first_party = first_party_top_names()
    per_file: Dict[str, int] = {}
    for path in iter_package_files():
        tree = parse_file(path)
        if tree is None:
            continue
        annotate_parents(tree)
        n = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = {top_package(a.name) for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    continue
                targets = {top_package(node.module)}
            else:
                continue
            if not (targets & first_party):
                continue
            if enclosing_function(node) is not None:
                n += 1
        if n:
            per_file[rel(path)] = n
    return sorted(per_file.items(), key=lambda kv: -kv[1])[:top_n]


# ── (c) Private-symbol cross-package imports ─────────────────────────────────


def measure_private_symbol_imports() -> List[str]:
    """Every cross-package ``from x.y import _private`` in first-party code.

    Entry format: ``"<file>::<module>::<symbol>"`` (stable, sorted). Dunder
    names (``__version__``) are not private by convention and are excluded.
    Cross-package means the import target's top-level namespace differs from
    the importing file's — ``from agent.core import _x`` inside ``agent/``
    is that package's own business.
    """
    first_party = first_party_top_names()
    entries: Set[str] = set()
    for path in iter_repo_source_files(include_tests=False):
        src_top = source_top(path)
        tree = parse_file(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
                continue
            target_top = top_package(node.module)
            if target_top not in first_party or target_top == src_top:
                continue
            for alias in node.names:
                name = alias.name
                if not name.startswith("_"):
                    continue
                if name.startswith("__") and name.endswith("__"):
                    continue  # dunder — public by convention
                entries.add(f"{rel(path)}::{node.module}::{name}")
    return sorted(entries)


# ── (d) Wired-security-control reference resolution ──────────────────────────


def _local_module_bindings(tree: ast.Module) -> Dict[str, str]:
    """Map local names → fully-qualified module names they're bound to.

    Covers ``import a.b.c`` (binds ``a``), ``import a.b.c as x`` (binds
    ``x`` → ``a.b.c``) and ``from a.b import c [as x]`` where ``c`` may be a
    submodule (binds ``c``/``x`` → ``a.b.c``).
    """
    bindings: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname] = alias.name
                else:
                    bindings[top_package(alias.name)] = top_package(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            for alias in node.names:
                bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bindings


def _dotted_name(node: ast.AST) -> Optional[str]:
    """Reassemble ``a.b.c`` from nested Attribute/Name nodes, else None."""
    parts: List[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


# Per-file reference index so resolving N registry symbols costs one walk
# per file total, not one walk per (file, symbol) pair.
# rel_path -> (importfrom {(module, name): {bound_names}}, bindings,
#              loaded_names, attribute_dotted_names)
_REF_INDEX: Dict[
    str,
    Tuple[Dict[Tuple[str, str], Set[str]], Dict[str, str], Set[str], Set[str]],
] = {}


def _reference_index(path: Path):
    key = rel(path)
    if key in _REF_INDEX:
        return _REF_INDEX[key]
    tree = parse_file(path)
    if tree is None:
        _REF_INDEX[key] = ({}, {}, set(), set())
        return _REF_INDEX[key]
    importfrom: Dict[Tuple[str, str], Set[str]] = {}
    loaded: Set[str] = set()
    dotteds: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and not node.level and node.module:
            for alias in node.names:
                importfrom.setdefault((node.module, alias.name), set()).add(
                    alias.asname or alias.name
                )
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            loaded.add(node.id)
        elif isinstance(node, ast.Attribute):
            dotted = _dotted_name(node)
            if dotted:
                dotteds.add(dotted)
    _REF_INDEX[key] = (importfrom, _local_module_bindings(tree), loaded, dotteds)
    return _REF_INDEX[key]


def find_symbol_references(module: str, symbol: str) -> List[str]:
    """Files (non-test, excluding the defining module) that *use* ``symbol``.

    A file counts when either:
    - it does ``from <module> import <symbol> [as x]`` AND the bound name
      appears as a load somewhere in the file, or
    - any dotted expression in it resolves — via that file's own imports —
      to ``<module>.<symbol>`` (covers ``import gateway.relay.auth as a;
      a.verify(...)`` and ``from gateway.relay import auth;
      auth.verify(...)``).

    Import-without-use deliberately does not count: a dangling import is not
    wiring.
    """
    module_file = REPO_ROOT / (module.replace(".", "/") + ".py")
    module_pkg_init = REPO_ROOT / module.replace(".", "/") / "__init__.py"
    hits: List[str] = []

    for path in iter_repo_source_files(include_tests=False):
        if path in (module_file, module_pkg_init):
            continue
        importfrom, bindings, loaded, dotteds = _reference_index(path)

        used = False
        for bound in importfrom.get((module, symbol), ()):  # from m import s
            if bound in loaded:
                used = True
                break
        if not used:
            suffix = f".{symbol}"
            for dotted in dotteds:
                if not dotted.endswith(suffix):
                    continue
                head = dotted[: -len(suffix)]
                if head == module:
                    used = True
                    break
                resolved = bindings.get(top_package(head))
                if resolved is not None and (
                    head.replace(top_package(head), resolved, 1) == module
                ):
                    used = True
                    break
        if used:
            hits.append(rel(path))
    return sorted(hits)


def find_internal_symbol_references(module: str, symbol: str) -> List[str]:
    """``module.py:LINE`` for uses of ``symbol`` *inside its own module*.

    Companion to :func:`find_symbol_references`, which deliberately skips the
    defining module because it answers "who imports this". That is the right
    question for a public cross-module API, but it makes a whole class of
    control invisible: a module-private guard invoked only by its own
    siblings — ``_check_slack_download_url`` (SSRF pin, called by the two
    Slack download helpers), ``_derive_payload_summary`` (confused-deputy
    guard, called by the approval recorder), ``neuter_async_httpx_del``
    (called by the async client factory next to it). Those have zero
    cross-module callers by design, so cross-module counting alone reports
    them "unwired" and cannot tell a private-but-active control apart from a
    genuinely dead one.

    Counts ``ast.Name`` loads only, so the symbol's own ``def``/``class``
    statement, prose mentions in docstrings, and comments are all naturally
    excluded — a docstring that merely *names* the control is exactly the
    false positive this must not produce.
    """
    module_file = REPO_ROOT / (module.replace(".", "/") + ".py")
    if not module_file.exists():
        module_file = REPO_ROOT / module.replace(".", "/") / "__init__.py"
    if not module_file.exists():
        return []
    tree = parse_file(module_file)
    if tree is None:
        return []
    rel_path = rel(module_file)
    lines = sorted(
        {
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id == symbol
        }
    )
    return [f"{rel_path}:{line}" for line in lines]


def find_any_symbol_references(module: str, symbol: str) -> List[str]:
    """Every non-test call site of ``symbol``, cross-module and internal.

    This is the honest answer to "is this control actually invoked in
    production code?", which is the only question the wired/unwired ratchet
    cares about. Callers wanting the narrower "who imports this" should use
    :func:`find_symbol_references`.
    """
    return find_symbol_references(module, symbol) + find_internal_symbol_references(
        module, symbol
    )


def symbol_docstrings(module: str, symbol: str) -> Tuple[Optional[str], Optional[str]]:
    """(function_docstring, module_docstring) for ``module.symbol``."""
    module_file = REPO_ROOT / (module.replace(".", "/") + ".py")
    if not module_file.exists():
        module_file = REPO_ROOT / module.replace(".", "/") / "__init__.py"
    if not module_file.exists():
        return None, None
    tree = parse_file(module_file)
    if tree is None:
        return None, None
    fn_doc: Optional[str] = None
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == symbol
        ):
            fn_doc = ast.get_docstring(node)
            break
    return fn_doc, ast.get_docstring(tree)


# ── Baseline I/O ─────────────────────────────────────────────────────────────


def load_baseline(name: str) -> dict:
    path = BASELINE_DIR / name
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_baseline(name: str, data: object) -> None:
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    path = BASELINE_DIR / name
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def _main(argv: List[str]) -> int:
    write = "--write-baselines" in argv
    bare = measure_bare_config_reads()
    deps = measure_dependency_direction()
    priv = measure_private_symbol_imports()

    print("== bare config.yaml reads (module: count) ==")
    for k, v in bare.items():
        print(f"  {k}: {v}")
    print("== cross-package import statements ==")
    for k, v in deps["cross_package_import_statements"].items():
        print(f"  {k}: {v}")
    print("== deferred (function-body) cross-package imports per package ==")
    for k, v in deps["deferred_cross_package_imports"].items():
        print(f"  {k}: {v}")
    print("== heaviest function-body importers (any first-party target) ==")
    for k, v in deferred_imports_by_file():
        print(f"  {k}: {v}")
    print(f"== cross-package private-symbol imports: {len(priv)} ==")
    for e in priv:
        print(f"  {e}")

    if write:
        write_baseline("bare_config_reads.json", bare)
        write_baseline("dependency_direction.json", deps)
        write_baseline("private_symbol_imports.json", priv)
        print(f"baselines written to {BASELINE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
