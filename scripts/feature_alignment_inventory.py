"""Inventory declared official HTTP/RPC surfaces without importing live services.

This is evidence collection, not a parity verdict: dynamic/plugin registration,
wire behavior and native interaction require additional runtime evidence.
"""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[1]
SURFACE_DIRS = ("hermes_cli", "tui_gateway")
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "websocket"}


def git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True, encoding="utf-8").strip()


def literal(node: ast.AST | None) -> str:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def declarations(source: str, path: str) -> dict[str, list[dict]]:
    tree = ast.parse(source, filename=path)
    prefixes: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
            if isinstance(call.func, ast.Name) and call.func.id == "APIRouter":
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        prefixes[target.id] = next((literal(k.value) for k in call.keywords if k.arg == "prefix"), "")
    found: dict[str, list[dict]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not decorator.args:
                continue
            name = literal(decorator.args[0])
            function = decorator.func
            key = ""
            if isinstance(function, ast.Name) and function.id == "method" and name:
                key = f"RPC {name}"
            elif isinstance(function, ast.Attribute) and function.attr in HTTP_METHODS and name.startswith("/"):
                owner = function.value.id if isinstance(function.value, ast.Name) else ""
                key = f"{function.attr.upper()} {prefixes.get(owner, '')}{name}"
            if key:
                found.setdefault(key, []).append({"path": path, "line": decorator.lineno, "handler": node.name})
    return found


def collect(sources) -> tuple[dict[str, list[dict]], int]:
    result: dict[str, list[dict]] = {}
    count = 0
    for path, source in sources:
        count += 1
        for key, value in declarations(source, path).items():
            result.setdefault(key, []).extend(value)
    return result, count


def official_sources(ref: str):
    archive = subprocess.check_output(["git", "-C", str(ROOT), "archive", ref, *SURFACE_DIRS])
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for member in bundle:
            if member.isfile() and member.name.endswith(".py"):
                stream = bundle.extractfile(member)
                assert stream is not None
                yield member.name, stream.read().decode("utf-8-sig")


def local_sources():
    for directory in SURFACE_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8-sig")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-ref", default="upstream/main")
    parser.add_argument("--ios-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Output JSON; Markdown uses the same stem")
    args = parser.parse_args()
    official_sha = git("rev-parse", "--verify", f"{args.official_ref}^{{commit}}")
    official, official_files = collect(official_sources(official_sha))
    backend, backend_files = collect(local_sources())
    specification = json.loads((args.ios_root / "docs/spec/swiftui-route-actions.json").read_text(encoding="utf-8"))
    ios_sha = subprocess.check_output(["git", "-C", str(args.ios_root), "rev-parse", "HEAD"], text=True).strip()
    rows = [{
        "capability": key,
        "official_declarations": official[key],
        "backend_declarations": backend.get(key, []),
        "backend_behavior": "not_verified",
        "ios_interface": "mapping_required",
        "ios_ui": "device_test_required",
        "ios_test": "not_verified_for_this_capability",
        "accepted": False,
        "issue": "runtime_and_ios_evidence_required" if key in backend else "backend_declaration_missing_or_moved",
    } for key in sorted(official)]
    document = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "official_sha": official_sha,
        "backend_sha": git("rev-parse", "HEAD"),
        "backend_dirty": bool(git("status", "--porcelain", "--untracked-files=normal")),
        "ios_sha": ios_sha,
        "ios_dirty": bool(subprocess.check_output(["git", "-C", str(args.ios_root), "status", "--porcelain"], text=True).strip()),
        "scope": list(SURFACE_DIRS),
        "limits": [
            "Static decorators only. Dynamic/plugin routes, tools and UI-only features require separate inventory.",
            "A declaration proves presence only, not mount/authentication/behavior/compatibility.",
            "Missing declarations can indicate moved or equivalent interfaces; triage before implementing.",
            "iOS action names are declarations, not evidence that an official capability is operable.",
        ],
        "summary": {
            "official_python_files": official_files,
            "backend_python_files": backend_files,
            "official_declarations": len(official),
            "matching_backend_declarations": len(official.keys() & backend.keys()),
            "missing_or_moved_backend_declarations": len(official.keys() - backend.keys()),
            "ios_declared_actions": len(specification["actions"]),
            "fully_accepted": 0,
        },
        "features": rows,
        "ios_actions": specification["actions"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Hermes Feature Alignment Inventory", "",
        f"Official: `{official_sha}`; backend: `{document['backend_sha']}`; iOS: `{ios_sha}`.", "",
        "Working trees may contain changes; see the JSON dirty flags. No row is fully accepted.", "",
        *[f"- {limit}" for limit in document["limits"]], "",
        "| Official capability | Official source | Backend declaration | Backend behavior | iOS API | iOS UI | iOS test | Issue |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        evidence = row["official_declarations"][0]
        presence = "present (static)" if row["backend_declarations"] else "missing/moved"
        lines.append(f"| `{row['capability']}` | `{evidence['path']}:{evidence['line']}` | {presence} | pending | mapping pending | device unavailable | pending | {row['issue']} |")
    lines += ["", "## Declared iOS Actions", "", "| Action | Contract key | Acceptance |", "| --- | --- | --- |"]
    for key, action in specification["actions"].items():
        lines.append(f"| `{action}` | `{key}` | runtime and device evidence required |")
    args.output.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(document["summary"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
