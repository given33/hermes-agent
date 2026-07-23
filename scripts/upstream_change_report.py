#!/usr/bin/env python3
"""Generate a deterministic risk report for an upstream Hermes tag."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


IOS_RISK_PREFIXES = (
    "dashboard/",
    "gateway/",
    "hermes_cli/",
    "plugins/",
    "tools/mcp_tool.py",
)
DEPLOYMENT_RISK_PREFIXES = (
    ".github/workflows/",
    "deploy/",
    "pyproject.toml",
    "scripts/install",
    "uv.lock",
)


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        text=True,
        encoding="utf-8",
        errors="replace",
    ).strip()


def changed_files(left: str, right: str) -> set[str]:
    output = git("diff", "--name-only", f"{left}..{right}")
    return {line.strip() for line in output.splitlines() if line.strip()}


def matches_any(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix) for prefix in prefixes)


def bullets(values: list[str], *, empty: str = "None") -> str:
    if not values:
        return f"- {empty}"
    return "\n".join(f"- `{value}`" for value in values)


def build_report(base: str, upstream_tag: str) -> str:
    merge_base = git("merge-base", base, upstream_tag)
    fork_files = changed_files(merge_base, base)
    upstream_files = changed_files(merge_base, upstream_tag)
    overlap = sorted(fork_files & upstream_files)
    ios_risk = sorted(path for path in upstream_files if matches_any(path, IOS_RISK_PREFIXES))
    deployment_risk = sorted(
        path for path in upstream_files if matches_any(path, DEPLOYMENT_RISK_PREFIXES)
    )
    commits = git(
        "log",
        "--no-merges",
        "--pretty=format:%h %s",
        f"{merge_base}..{upstream_tag}",
    ).splitlines()
    commits = commits[:200]
    verdict = (
        "Manual Codex review required before merge."
        if overlap or ios_risk or deployment_risk
        else "No known product overlap; Codex still verifies CI before merge."
    )
    return f"""# Upstream Hermes sync report

- Upstream: `NousResearch/hermes-agent@{upstream_tag}`
- Product base: `{base}`
- Merge base: `{merge_base}`
- Upstream commits: `{len(commits)}` (report capped at 200)
- Upstream files changed: `{len(upstream_files)}`
- Fork files changed since merge base: `{len(fork_files)}`
- Direct file overlap: `{len(overlap)}`
- Gate: **{verdict}**

## Product overlap

{bullets(overlap)}

## iOS/API adaptation signals

Changes here can affect mobile authentication, hosted conversations, MCP discovery,
dashboard contracts, or server event rendering. They require an explicit iOS impact
decision even when the merge itself is conflict-free.

{bullets(ios_risk)}

## Deployment and dependency signals

{bullets(deployment_risk)}

## Upstream commits

{bullets(commits)}

## Required gates

- [ ] Merge completed without unresolved conflicts.
- [ ] Official Hermes CI is green on the sync pull request.
- [ ] Collaboration, mobile auth, deployment, MCP, and iOS contract tests are green.
- [ ] Codex reviewed direct overlap and the generated risk sections.
- [ ] Any required Hermes iOS adaptation was explicitly accepted or deferred.
- [ ] The approved `main` commit was deployed transactionally to the main server.
- [ ] DBB3 and WSL report the exact approved commit and pass health probes.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(args.base, args.tag)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
