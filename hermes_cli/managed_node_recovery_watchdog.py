"""Periodically evaluate managed-node health and trigger configured recovery."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from typing import Any

from hermes_cli.managed_nodes import fetch_managed_nodes


def run_watchdog(
    config_path: Path | None = None,
    *,
    interval_seconds: float = 30.0,
    state_file: Path | None = None,
    once: bool = False,
) -> dict[str, Any]:
    """Run the independent recovery loop, returning the latest safe snapshot."""

    interval = max(5.0, float(interval_seconds))
    latest: dict[str, Any] = {"configured": False, "nodes": [], "sources": []}
    while True:
        try:
            latest = fetch_managed_nodes(config_path)
        except Exception as exc:  # configuration errors must not kill recovery
            latest = {
                "configured": False,
                "nodes": [],
                "sources": [{
                    "id": "managed-nodes",
                    "online": False,
                    "error": type(exc).__name__,
                }],
            }
        if state_file:
            _write_state(state_file, latest)
        if once:
            return latest
        time.sleep(interval)


def _write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=True, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes managed-node recovery watchdog")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    run_watchdog(
        args.config,
        interval_seconds=args.interval,
        state_file=args.state_file,
        once=args.once,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
