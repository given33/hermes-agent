#!/usr/bin/env python3
"""Backup Hermes hosted-state files (SQLite WAL + single.json) safely.

This is the operational backup helper for the cloud hosting chain.  It takes
SQLite backups through the ``sqlite3`` online backup API so the copies are
consistent even while the server is running, then atomically snapshots the
JSON collaboration documents.

Usage:
    python scripts/backup_hosted_state.py --output-dir /var/backups/hermes
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover - fallback for standalone use
    def get_hermes_home() -> Path:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _backup_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not source.is_file():
        return
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(temp_path)
            try:
                source.backup(dst)
                dst.commit()
            finally:
                dst.close()
        finally:
            src.close()
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


def _backup_json(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not source.is_file():
        return
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        shutil.copy2(source, temp_path)
        with temp_path.open("rb") as handle:
            json.load(handle)
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--home", default="")
    args = parser.parse_args(argv)

    home = Path(args.home) if args.home else get_hermes_home()
    output = Path(args.output_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = output / f"hosted-state-{stamp}"
    target.mkdir(parents=True, exist_ok=True)

    sources = {
        "collaboration/single.json": home / "collaboration" / "single.json",
        "collaboration/rooms.json": home / "collaboration" / "rooms.json",
        "collaboration/account-files/library.sqlite3": home
        / "collaboration"
        / "account-files"
        / "library.sqlite3",
        "dashboard/mobile-auth.db": home / "dashboard" / "mobile-auth.db",
    }
    backed_up: list[str] = []
    for rel, source in sources.items():
        dest = target / rel
        if source.suffix == ".json":
            _backup_json(source, dest)
        else:
            _backup_sqlite(source, dest)
        if dest.is_file():
            backed_up.append(rel)

    manifest = {
        "schema": "hermes.hosted-state-backup.v1",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hermes_home": str(home),
        "files": backed_up,
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, "backup_dir": str(target), "files": backed_up}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
