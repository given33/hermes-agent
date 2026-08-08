"""Clone and prepare the private Hemres Pi source used by Hermes Coding.

The script never stores GitHub credentials. Git must already be authenticated
through a deploy key, credential helper, GitHub CLI setup, or the deployment
platform's secret-backed Git transport.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


DEFAULT_REPOSITORY = "https://github.com/given33/hemres-pi.git"
DEFAULT_REF = "3a8591a8af5b6d200088d12ca75a5517cb064fa8"


def run(command: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def sync_source(repository: str, source_ref: str, root: Path) -> None:
    root = root.expanduser().resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    if (root / ".git").is_dir():
        remote = run(["git", "-C", str(root), "config", "--get", "remote.origin.url"])
        if normalize_remote(remote) != normalize_remote(repository):
            raise RuntimeError(f"Existing Pi checkout remote mismatch: {remote or '<none>'}")
        run(["git", "-C", str(root), "fetch", "--depth", "1", "origin", source_ref])
        run(["git", "-C", str(root), "checkout", "--force", "FETCH_HEAD"])
    else:
        run(["git", "clone", "--depth", "1", repository, str(root)])
        current = run(["git", "-C", str(root), "rev-parse", "HEAD"])
        if current.lower() != source_ref.lower():
            run(["git", "-C", str(root), "fetch", "--depth", "1", "origin", source_ref])
            run(["git", "-C", str(root), "checkout", "--force", "FETCH_HEAD"])

    actual = run(["git", "-C", str(root), "rev-parse", "HEAD"])
    if actual.lower() != source_ref.lower():
        raise RuntimeError(f"Pi source ref mismatch: expected {source_ref}, found {actual}")

    bun = os.environ.get("CODING_PI_BUN_PATH", "bun")
    run([bun, "install", "--frozen-lockfile"], cwd=root)
    run([bun, "--cwd=packages/coding-agent", "run", "gen:tool-views"], cwd=root)
    run([bun, "run", "build:native"], cwd=root)


def normalize_remote(value: str) -> str:
    normalized = value.strip().rstrip("/").lower()
    return normalized[:-4] if normalized.endswith(".git") else normalized


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--ref", default=DEFAULT_REF)
    parser.add_argument("--root", required=True, help="Destination checkout used by Hermes coding_pi.root")
    args = parser.parse_args()
    sync_source(args.repository, args.ref, Path(args.root))
    print(f"Prepared {args.repository}@{args.ref} at {Path(args.root).expanduser().resolve()}")


if __name__ == "__main__":
    main()
