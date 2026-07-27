"""Cross-platform process inspection primitives with no service-layer imports."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess


_IS_WINDOWS = os.name == "nt"


def pid_exists(pid: int) -> bool:
    """Return whether *pid* is alive without signalling it on Windows.

    ``os.kill(pid, 0)`` maps to ``CTRL_C_EVENT`` on Windows and can terminate
    a console process group. Prefer psutil, then use a non-signalling native
    probe on Windows and the conventional signal-zero check on POSIX.
    Zombies are treated as dead on every supported path.
    """
    try:
        import psutil  # type: ignore

        try:
            if psutil.Process(int(pid)).status() == psutil.STATUS_ZOMBIE:
                return False
        except getattr(psutil, "NoSuchProcess", ()):
            return False
        except Exception:
            pass
        return bool(psutil.pid_exists(int(pid)))
    except ImportError:
        pass

    if _IS_WINDOWS:
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.WaitForSingleObject.restype = ctypes.c_uint
            kernel32.GetLastError.restype = ctypes.c_uint
            process_query_limited_information = 0x1000
            synchronize = 0x100000
            wait_timeout = 0x00000102
            error_invalid_parameter = 87
            error_access_denied = 5
            handle = kernel32.OpenProcess(
                process_query_limited_information | synchronize,
                False,
                int(pid),
            )
            if not handle:
                error = kernel32.GetLastError()
                if error == error_invalid_parameter:
                    return False
                if error == error_access_denied:
                    return True
                return False
            try:
                return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
            finally:
                kernel32.CloseHandle(handle)
        except (OSError, AttributeError):
            return False

    try:
        stat_fields = Path(f"/proc/{int(pid)}/stat").read_text(
            encoding="utf-8"
        ).split()
        if len(stat_fields) > 2 and stat_fields[2] == "Z":
            return False
    except FileNotFoundError:
        try:
            result = subprocess.run(
                ["ps", "-o", "state=", "-p", str(int(pid))],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip().startswith("Z"):
                return False
        except Exception:
            pass
    except (IndexError, PermissionError, OSError):
        pass

    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
