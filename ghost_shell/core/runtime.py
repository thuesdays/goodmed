"""
runtime.py — Shared helpers for runtime metadata (PIDs, shutdown tokens).

Background
==========
The Ghost Shell installer (Inno Setup) needs to stop the dashboard server
and the scheduler before it can replace files during an update. To do that
gracefully it needs three things from a known location:

  • the PID of the running process (so `taskkill` works as fallback)
  • the port (so the installer can hit a graceful HTTP shutdown endpoint)
  • a one-time shutdown token (so the endpoint cannot be tricked from
    a hostile webpage running in the user's main browser via fetch())

This module owns the directory layout. Both the dashboard and the
scheduler import from here so the installer side can rely on a single
canonical location.

Runtime directory
=================
Windows : %LOCALAPPDATA%\\GhostShellAnty\\
macOS   : ~/Library/Application Support/GhostShellAnty/
Linux   : $XDG_RUNTIME_DIR or ~/.local/share/GhostShellAnty/

Files written
=============
  runtime.json   — dashboard server { pid, port, shutdown_token, started_at }
  scheduler.pid  — scheduler PID (one line, integer)
  backup/        — pre-update DB backups (timestamped)
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import os
import json
import secrets
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional


# ──────────────────────────────────────────────────────────────
# Runtime dir resolution
# ──────────────────────────────────────────────────────────────

APP_DIR_NAME = "GhostShellAnty"


def runtime_dir() -> str:
    """
    Return (and create if needed) the per-user runtime/state directory
    where short-lived metadata like PIDs and shutdown tokens live.

    Convention matches what the Inno Setup installer reads from. If you
    change the directory layout here, also update `installer/ghost_shell_installer.iss`
    and `installer/build.bat`.
    """
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            base = os.path.join(os.path.expanduser("~"), "AppData", "Local")
        path = os.path.join(base, APP_DIR_NAME)
    elif sys.platform == "darwin":
        path = os.path.join(
            os.path.expanduser("~"), "Library", "Application Support",
            APP_DIR_NAME
        )
    else:
        # Prefer XDG_RUNTIME_DIR (per-session, tmpfs) but fall back to
        # XDG_DATA_HOME / ~/.local/share so the location persists across
        # logins on systems without runtime dirs (Docker, etc.).
        base = os.environ.get("XDG_RUNTIME_DIR")
        if not base or not os.path.isdir(base):
            base = os.environ.get("XDG_DATA_HOME") or \
                   os.path.join(os.path.expanduser("~"), ".local", "share")
        path = os.path.join(base, APP_DIR_NAME)

    os.makedirs(path, exist_ok=True)
    return path


def runtime_path(*parts: str) -> str:
    """Convenience: join one or more components onto runtime_dir()."""
    return os.path.join(runtime_dir(), *parts)


def backup_dir() -> str:
    """
    Returns (and creates if needed) the directory where the installer
    drops timestamped DB backups before an Update / Reinstall.
    """
    path = runtime_path("backup")
    os.makedirs(path, exist_ok=True)
    return path


# ──────────────────────────────────────────────────────────────
# runtime.json — dashboard server's "I'm alive" file
# ──────────────────────────────────────────────────────────────

RUNTIME_JSON = "runtime.json"


def _atomic_write(path: str, data: str) -> None:
    """
    Write a file atomically: tempfile → fsync → rename. Avoids the case
    where the installer reads a half-written runtime.json mid-startup.
    """
    dirpath = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".rt-", dir=dirpath)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_runtime_info(port: int, install_dir: Optional[str] = None) -> dict:
    """
    Called by the dashboard server right after it knows what port it'll
    bind to. Generates a fresh shutdown token, writes runtime.json,
    returns the dict so the caller can stash the token in-memory for
    later validation.

    Existing file is overwritten — only one dashboard process should be
    running at a time, and if a stale file is left behind from a crash
    we want the new one to win.
    """
    info = {
        "pid":             os.getpid(),
        "port":            int(port),
        "shutdown_token":  secrets.token_urlsafe(24),
        "started_at":      int(time.time()),
        "install_dir":     install_dir or "",
        "version":         _read_version_safe(),
    }
    _atomic_write(runtime_path(RUNTIME_JSON), json.dumps(info, indent=2))
    return info


def read_runtime_info() -> Optional[dict]:
    """
    Read runtime.json — returns None if missing or unparseable. Used by
    the installer (via Pascal Script) and by health checks.
    """
    p = runtime_path(RUNTIME_JSON)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def clear_runtime_info() -> None:
    """Remove runtime.json. Safe to call when the file isn't there."""
    try:
        os.unlink(runtime_path(RUNTIME_JSON))
    except FileNotFoundError:
        pass
    except OSError:
        pass


# ──────────────────────────────────────────────────────────────
# Generic PID file (used by scheduler — no shutdown endpoint needed)
# ──────────────────────────────────────────────────────────────

def write_pid_file(name: str) -> str:
    """
    Write the current process's PID to <runtime_dir>/<name>. The installer
    reads these files to know which processes to terminate.

    Returns the absolute path so the caller can clean up on exit.
    """
    path = runtime_path(name)
    _atomic_write(path, str(os.getpid()))
    return path


def read_pid_file(name: str) -> Optional[int]:
    """Read a PID file; returns None if missing/empty/non-int."""
    path = runtime_path(name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def clear_pid_file(name: str) -> None:
    try:
        os.unlink(runtime_path(name))
    except FileNotFoundError:
        pass
    except OSError:
        pass


# ──────────────────────────────────────────────────────────────
# Misc helpers
# ──────────────────────────────────────────────────────────────

def _read_version_safe() -> str:
    """
    Best-effort fetch of the package version for the runtime info dump.
    Stays optional — installer doesn't depend on it being present.
    """
    try:
        from ghost_shell import __version__  # noqa: WPS433
        return str(__version__)
    except Exception:
        pass
    try:
        # Fallback: read .build_number from project root (installer drops it)
        p = Path(__file__).resolve().parents[2] / ".build_number"
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def is_pid_alive(pid: int) -> bool:
    """
    Cross-platform PID liveness check. Used by the installer-side helper
    to decide whether to escalate from graceful HTTP → taskkill → /F.
    """
    if not pid or pid <= 0:
        return False
    if sys.platform.startswith("win"):
        # Windows: open the process; ERROR_INVALID_PARAMETER = no such PID,
        # ERROR_ACCESS_DENIED = exists but not ours.
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            if not h:
                # ERROR_ACCESS_DENIED = 5 → exists, just not visible to us
                return ctypes.GetLastError() == 5
            try:
                code = ctypes.c_ulong(0)
                kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(h)
        except Exception:
            return False
    else:
        try:
            os.kill(int(pid), 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False
