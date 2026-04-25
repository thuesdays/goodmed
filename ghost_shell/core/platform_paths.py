"""
platform_paths.py — Cross-platform helpers for Ghost Shell.

Handles the differences between Windows and macOS/Linux for:
  - executable extensions (.exe vs no extension)
  - Chromium binary location (flat vs .app bundle on macOS)
  - chromedriver filename
  - per-platform default paths

Usage:
    from ghost_shell.core.platform_paths import PLATFORM, exe_ext, find_chrome_binary

    if PLATFORM == "darwin":
        ...
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import os
import sys
import platform as _platform


# "windows" | "darwin" | "linux"
PLATFORM = (
    "windows" if sys.platform.startswith("win")
    else "darwin" if sys.platform == "darwin"
    else "linux"
)

IS_WINDOWS = PLATFORM == "windows"
IS_MACOS   = PLATFORM == "darwin"
IS_LINUX   = PLATFORM == "linux"

# ──────────────────────────────────────────────────────────────
# PROJECT_ROOT — absolute path to the directory that contains the
# ghost_shell package. After the package refactor (v0.2.0) a lot of
# modules need to resolve paths relative to the *repo root* rather
# than their own file location (which is now nested inside the
# package). Historically this was `os.path.dirname(__file__)` of
# modules that lived at the top level; now that they're inside
# ghost_shell/, go up 3 levels: file → core/ → ghost_shell/ → repo.
#
# This is the single source of truth — other modules import it.
# ──────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))




def exe_ext() -> str:
    """'.exe' on Windows, empty string elsewhere."""
    return ".exe" if IS_WINDOWS else ""


def default_chrome_subdir() -> str:
    """
    The standard per-platform subdir name for our deployed Chromium build.
    We keep this configurable via `browser.binary_path` in the DB, but this
    is what the defaults expand to.
    """
    return {
        "windows": "chrome_win64",
        "darwin":  "chrome_mac",
        "linux":   "chrome_linux",
    }[PLATFORM]


def find_chrome_binary(base_dir: str = None) -> str | None:
    """
    Resolve the Chromium binary path for the current platform.

    base_dir is the deployment directory (e.g. 'chrome_mac'); if None,
    we try the platform default.

    Returns the full path if found, else None.

    Windows: <base_dir>/chrome.exe
    Linux  : <base_dir>/chrome        (custom build) or 'chromium'
    macOS  : <base_dir>/Chromium.app/Contents/MacOS/Chromium
             or <base_dir>/Google Chrome.app/Contents/MacOS/Google Chrome
             or <base_dir>/chrome     (flat build — uncommon on mac)
    """
    if base_dir is None:
        base_dir = default_chrome_subdir()

    if not os.path.isabs(base_dir):
        # Resolve relative to current working directory
        base_dir = os.path.abspath(base_dir)

    if IS_WINDOWS:
        candidates = [
            os.path.join(base_dir, "chrome.exe"),
        ]
    elif IS_MACOS:
        candidates = [
            os.path.join(base_dir, "Chromium.app", "Contents", "MacOS", "Chromium"),
            os.path.join(base_dir, "Google Chrome.app", "Contents", "MacOS",
                         "Google Chrome"),
            os.path.join(base_dir, "chrome"),
            os.path.join(base_dir, "chromium"),
        ]
    else:  # linux
        candidates = [
            os.path.join(base_dir, "chrome"),
            os.path.join(base_dir, "chromium"),
            os.path.join(base_dir, "chromium-browser"),
        ]

    for p in candidates:
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return None


def find_chromedriver(base_dir: str = None) -> str | None:
    """
    Find chromedriver next to (or inside) the Chromium build directory.

    Looks for:
      - <base_dir>/chromedriver[.exe]
      - On macOS also checks <base_dir>/Chromium.app/Contents/MacOS/chromedriver
    """
    if base_dir is None:
        base_dir = default_chrome_subdir()
    if not os.path.isabs(base_dir):
        base_dir = os.path.abspath(base_dir)

    name = "chromedriver" + exe_ext()
    candidates = [os.path.join(base_dir, name)]
    if IS_MACOS:
        candidates.extend([
            os.path.join(base_dir, "Chromium.app", "Contents", "MacOS", name),
            os.path.join(base_dir, "Google Chrome.app", "Contents", "MacOS", name),
        ])

    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def popen_flags_no_console() -> dict:
    """
    Return platform-appropriate kwargs for subprocess.Popen that (a) do NOT
    pop up an extra console window on Windows, and (b) put the child in its
    own process group on Unix so we can kill the whole tree.
    """
    import subprocess
    kw = {}
    if IS_WINDOWS:
        kw["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    else:
        # On Unix, start_new_session=True does setsid() — children get
        # a new process group, so os.killpg() can terminate the whole tree.
        kw["start_new_session"] = True
    return kw


def terminate_process_tree(proc):
    """
    Kill a subprocess.Popen and all its descendants (chrome, chromedriver,
    renderers, etc.). Uses psutil if available for the cleanest job.
    """
    if proc is None or proc.poll() is not None:
        return 0
    killed = 0
    try:
        import psutil
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            try:
                child.terminate()
                killed += 1
            except Exception:
                pass
        try:
            parent.terminate()
            killed += 1
        except Exception:
            pass
        # Wait a little, then SIGKILL anything still alive
        import time as _t
        _t.sleep(0.5)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except Exception:
                pass
        try:
            parent.kill()
        except Exception:
            pass
    except ImportError:
        # Fallback without psutil
        try:
            if IS_WINDOWS:
                import signal as _s
                proc.send_signal(_s.CTRL_BREAK_EVENT)
            else:
                import os as _os, signal as _s
                _os.killpg(_os.getpgid(proc.pid), _s.SIGTERM)
            killed += 1
        except Exception:
            try:
                proc.terminate()
                killed += 1
            except Exception:
                pass
    return killed


if __name__ == "__main__":
    # Quick diagnostic when the user runs this file directly.
    print(f"Platform:         {PLATFORM}")
    print(f"exe_ext():        '{exe_ext()}'")
    print(f"Default subdir:   {default_chrome_subdir()}")
    print(f"Chrome found:     {find_chrome_binary()}")
    print(f"Driver found:     {find_chromedriver()}")
