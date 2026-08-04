"""Runtime-owned Python package installation strategy.

The CLI post-setup flow and lower-layer runtime features both need the same
``uv -> pip -> ensurepip`` fallback. Keeping that policy here prevents agent
features from importing a private CLI helper merely to install an optional
dependency.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Sequence

from hermes_runtime.subprocess_compat import resolve_managed_uv, windows_hide_flags

__all__ = ["install_python_packages"]


def install_python_packages(
    args: Sequence[str],
    *,
    timeout: int = 300,
    capture_output: bool = True,
    creationflags: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Install packages using the active Hermes Python environment."""

    flags = windows_hide_flags() if creationflags is None else creationflags
    install_args = [str(arg) for arg in args]
    venv_root = Path(sys.executable).parent.parent
    uv_env = {**os.environ, "VIRTUAL_ENV": str(venv_root)}

    uv_bin = resolve_managed_uv()
    if uv_bin is None:
        # A package-manager uv (for example Termux's) is a valid fallback when
        # Hermes has not provisioned its private binary yet. Keep the managed
        # path authoritative whenever it exists.
        path_lookup = getattr(shutil, "which")
        uv_bin = path_lookup("uv")
    if uv_bin:
        try:
            result = subprocess.run(
                [uv_bin, "pip", "install", *install_args],
                capture_output=capture_output,
                text=True,
                timeout=timeout,
                env=uv_env,
                creationflags=flags,
            )
            if result.returncode == 0:
                return result
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    pip_cmd = [sys.executable, "-m", "pip"]
    try:
        probe = subprocess.run(
            [*pip_cmd, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=flags,
        )
        if probe.returncode != 0:
            raise FileNotFoundError("pip not in venv")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        try:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ensurepip",
                    "--upgrade",
                    "--default-pip",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
                creationflags=flags,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(
                pip_cmd,
                returncode=1,
                stdout="",
                stderr=f"pip not available and ensurepip failed: {exc}",
            )

    return subprocess.run(
        [*pip_cmd, "install", *install_args],
        capture_output=capture_output,
        text=True,
        timeout=timeout,
        creationflags=flags,
    )
