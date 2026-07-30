from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
import time


def never_return(args: dict, **_kwargs):
    started_path = str(args.get("started_path") or "")
    if started_path:
        Path(started_path).write_text("started", encoding="utf-8")
    while True:
        time.sleep(0.1)


def delayed_write(args: dict, **_kwargs):
    started_path = str(args.get("started_path") or "")
    if started_path:
        Path(started_path).write_text("started", encoding="utf-8")
    time.sleep(float(args.get("delay") or 0.2))
    Path(str(args["target"])).write_text("late-side-effect", encoding="utf-8")
    return "late-result"


def spawn_delayed_descendant(args: dict, **_kwargs):
    """Spawn a process that would perform a late write without tree teardown."""

    target = str(args["target"])
    started_path = str(args["descendant_started_path"])
    delay = str(float(args.get("delay") or 1.2))
    code = (
        "from pathlib import Path\n"
        "import os, sys, time\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(float(sys.argv[3]))\n"
        "Path(sys.argv[2]).write_text('late-descendant-side-effect', encoding='utf-8')\n"
    )
    subprocess.Popen([sys.executable, "-c", code, started_path, target, delay])
    started = str(args.get("handler_started_path") or "")
    if started:
        Path(started).write_text("started", encoding="utf-8")
    while True:
        time.sleep(0.1)


def spawn_detached_delayed_descendant(args: dict, **_kwargs):
    """Spawn a new-session child; tree teardown must still stop it."""

    target = str(args["target"])
    started_path = str(args["descendant_started_path"])
    delay = str(float(args.get("delay") or 1.2))
    code = (
        "from pathlib import Path\n"
        "import os, sys, time\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(float(sys.argv[3]))\n"
        "Path(sys.argv[2]).write_text('late-detached-side-effect', encoding='utf-8')\n"
    )
    subprocess.Popen(
        [sys.executable, "-c", code, started_path, target, delay],
        start_new_session=True,
    )
    while True:
        time.sleep(0.1)


def echo(args: dict, **_kwargs):
    return f"echo:{args.get('value', '')}"


async def async_echo(args: dict, **_kwargs):
    await asyncio.sleep(0)
    return f"async:{args.get('value', '')}"
