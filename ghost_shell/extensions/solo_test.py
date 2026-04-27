"""
solo_test.py — Spawn an isolated Chrome instance loading exactly ONE
extension and report whether it loads cleanly.

Why this exists
───────────────
Our manifest validation gate (see ``ghost_shell/browser/runtime.py``,
"HARD VALIDATION GATE") catches manifests that don't parse as JSON or
are missing required fields. It does NOT catch the harder class of
failures where Chrome accepts the manifest at JSON-level but rejects
the extension at runtime: missing ``background.service_worker`` file,
broken ``default_locale`` pointing at empty ``messages.json``, missing
icon files referenced from the manifest, ``manifest_version: 4`` (we
haven't validated yet), CRX2 signature on a payload uploaded as
unpacked, etc.

In a normal profile launch with ``--load-extension=ext1,ext2,ext3``,
ONE such broken extension takes down the whole session: Chrome rejects
the entire flag, exits 2-3s in, all extensions fail to load.

solo_test isolates the test: spawn a one-shot Chrome with **only this
one** extension, no profile carryover, no behavior overrides. If it
survives a few seconds → loads. If it dies → we read the chrome_debug
log and surface the actual error.

Public API
──────────
  test_extension(ext_id: str, timeout: float = 8.0) -> dict
  test_extension_path(pool_path: str, timeout: float = 8.0) -> dict

Result shape:

    {
      "ok":        bool,
      "status":    "loads" | "warnings" | "fails" | "no_chrome" |
                   "no_pool" | "not_found" | "error",
      "duration":  float (seconds),
      "errors":    [str, ...],           # extension-load errors
      "warnings":  [str, ...],           # extension-load warnings
      "exit_code": int | None,           # if Chrome exited
      "log_excerpt": str,                # last ~30 lines of chrome.log
      "reason":    str,                  # one-line summary for UI
    }
"""

from __future__ import annotations

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Optional

from ghost_shell.core.platform_paths import (
    find_chrome_binary, popen_flags_no_console,
)


# ──────────────────────────────────────────────────────────────
# Log parsing — patterns for "extension wouldn't load" indicators
# in chrome_debug.log. Patterns are loose on purpose; Chrome's
# error wording drifts between versions and locales.
# ──────────────────────────────────────────────────────────────

_ERROR_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"Extension load(ing)? (failed|error)",
        r"Could not load extension",
        r"Failed to load extension",
        r"Cannot load extension",
        r"Manifest is not a valid JSON object",
        r"Manifest file is missing",
        r"Manifest file is invalid",
        r"Service worker registration failed",
        r"Default locale (file )?not found",
        r"unable to load (the )?manifest",
        r"Required value '[^']+' is missing or invalid",
        r"Could not load javascript .* for extension",
        r"Could not load icon",
        r"Invalid value for '(?:matches|content_scripts|host_permissions)'",
    ]
]

_WARNING_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"Extension warning",
        r"Permission '[^']+' is unknown or URL pattern is malformed",
        r"Unrecognized manifest key",
        r"Unrecognized matches pattern",
    ]
]


def _scan_log(log_text: str) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) found in a chrome_debug.log dump."""
    errors: list[str] = []
    warnings: list[str] = []
    if not log_text:
        return errors, warnings
    for raw in log_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Errors take precedence — a line matching both buckets goes
        # to errors only.
        if any(p.search(line) for p in _ERROR_PATTERNS):
            errors.append(line[:300])
            continue
        if any(p.search(line) for p in _WARNING_PATTERNS):
            warnings.append(line[:300])
    # Dedupe while preserving order
    def _dedupe(xs):
        seen = set(); out = []
        for x in xs:
            if x not in seen:
                seen.add(x); out.append(x)
        return out
    return _dedupe(errors)[:10], _dedupe(warnings)[:10]


def _read_log_safely(path: str) -> str:
    """Read chrome_debug.log; never raise. Chrome may still hold the
    file briefly on Windows after process exit — retry once."""
    if not path or not os.path.exists(path):
        return ""
    for attempt in range(2):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            if attempt == 0:
                time.sleep(0.4)
                continue
            return ""
    return ""


def _tail(text: str, n_lines: int = 30) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-n_lines:])


# ──────────────────────────────────────────────────────────────
# Public test functions
# ──────────────────────────────────────────────────────────────

def test_extension(ext_id: str, timeout: float = 8.0) -> dict:
    """Look up the extension in DB by id, then test_extension_path()
    its pool_path. Convenience wrapper for the dashboard endpoint."""
    try:
        from ghost_shell.db.database import get_db
        row = get_db().extension_get(ext_id)
    except Exception as e:
        return _result("error", reason=f"DB lookup failed: {e}")

    if not row:
        return _result("not_found",
                       reason=f"extension {ext_id!r} is not in the pool")

    pool_path = row.get("pool_path")
    if not pool_path:
        return _result("no_pool",
                       reason=f"extension {ext_id!r} has no pool_path "
                              f"recorded in DB")

    return test_extension_path(pool_path, timeout=timeout)


def test_extension_path(pool_path: str, timeout: float = 8.0) -> dict:
    """Spawn an isolated Chrome with ``--load-extension=<pool_path>``
    and watch what happens. Returns the result dict (see module
    docstring)."""

    if not pool_path or not os.path.isdir(pool_path):
        return _result("no_pool",
                       reason=f"pool_path missing or not a directory: "
                              f"{pool_path}")

    chrome = find_chrome_binary()
    if not chrome:
        return _result("no_chrome",
                       reason="Chromium binary not found — set "
                              "browser.binary_path or run "
                              "scripts/download_chromium.")

    tmpdir = tempfile.mkdtemp(prefix="gs_ext_solo_")
    log_path = os.path.join(tmpdir, "chrome_debug.log")
    proc: Optional[subprocess.Popen] = None
    started_at = time.time()

    try:
        # Conservative arg list — minimal to keep the test fast and
        # focused on extension load behaviour. NO --ghost-shell-payload:
        # we're testing the extension, not the C++ stealth layer (and
        # we don't have a profile context to build a payload from).
        args = [
            chrome,
            f"--user-data-dir={tmpdir}",
            f"--load-extension={pool_path}",
            f"--disable-extensions-except={pool_path}",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-pings",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-sync",
            "--disable-translate",
            "--disable-features=Translate,AutofillServerCommunication",
            "--enable-logging",
            f"--log-file={log_path}",
            "--v=1",
            "--headless=new",      # invisible — we don't need a window
            "about:blank",
        ]

        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **popen_flags_no_console(),
        )

        # Poll for process exit OR timeout. 200ms granularity is fine.
        deadline = started_at + timeout
        exit_code: Optional[int] = None
        while time.time() < deadline:
            exit_code = proc.poll()
            if exit_code is not None:
                break
            time.sleep(0.2)

        duration = round(time.time() - started_at, 2)

        # Read the log AFTER process exit (or timeout), so all writes
        # are flushed. Chrome may still hold the file briefly on
        # Windows — _read_log_safely retries once.
        log_text = ""
        if exit_code is not None:
            # Process is dead, log fully written
            log_text = _read_log_safely(log_path)
        else:
            # Process is alive — kill gracefully so it can flush
            try:
                from ghost_shell.core.process_reaper import kill_process_tree
                kill_process_tree(proc.pid, reason="solo test cleanup")
            except Exception:
                try: proc.kill()
                except Exception: pass
            # Brief wait for log flush
            time.sleep(0.4)
            log_text = _read_log_safely(log_path)

        errors, warnings = _scan_log(log_text)
        excerpt = _tail(log_text, 30)

        # Verdict
        if exit_code is not None:
            # Chrome exited before our timeout. Most often = load
            # rejection. Sometimes (--headless=new on weird builds) =
            # immediate clean-exit; distinguish by checking for known
            # error patterns + non-zero exit code.
            if errors:
                return _result(
                    "fails", duration=duration, exit_code=exit_code,
                    errors=errors, warnings=warnings,
                    log_excerpt=excerpt,
                    reason=(f"Chrome exited in {duration}s with "
                            f"extension load errors. First: "
                            f"{errors[0][:120]}"),
                )
            if exit_code != 0:
                return _result(
                    "fails", duration=duration, exit_code=exit_code,
                    errors=errors, warnings=warnings,
                    log_excerpt=excerpt,
                    reason=(f"Chrome exited with code {exit_code} in "
                            f"{duration}s — no specific error pattern "
                            f"matched, see log_excerpt."),
                )
            # Clean exit, no errors — odd but acceptable. Treat as
            # loads-with-caveat.
            return _result(
                "warnings" if warnings else "loads",
                duration=duration, exit_code=exit_code,
                errors=errors, warnings=warnings,
                log_excerpt=excerpt,
                reason=(f"Chrome exited cleanly in {duration}s. "
                        f"{len(warnings)} warning(s)."),
            )

        # Process survived the timeout — extension is loaded happily
        if errors:
            # Extension loaded but emitted errors anyway (e.g.
            # service_worker failed to register but extension still
            # available). Surface as warnings status.
            return _result(
                "warnings", duration=duration, exit_code=None,
                errors=errors, warnings=warnings,
                log_excerpt=excerpt,
                reason=(f"Chrome stayed alive for {duration}s but the "
                        f"extension log shows {len(errors)} error(s). "
                        f"Loaded with degraded behaviour."),
            )
        return _result(
            "loads", duration=duration, exit_code=None,
            errors=[], warnings=warnings,
            log_excerpt=excerpt,
            reason=(f"Loaded cleanly in {duration}s"
                    f"{f' with {len(warnings)} warning(s)' if warnings else ''}."),
        )

    except Exception as e:
        logging.exception("[solo_test] unexpected failure")
        return _result(
            "error", duration=round(time.time() - started_at, 2),
            reason=f"solo test crashed: {type(e).__name__}: {e}",
        )

    finally:
        # Ensure no orphan Chrome from this test
        if proc is not None and proc.poll() is None:
            try:
                from ghost_shell.core.process_reaper import kill_process_tree
                kill_process_tree(proc.pid, reason="solo test final cleanup")
            except Exception:
                try: proc.kill()
                except Exception: pass

        # Wipe the temp profile dir. shutil.rmtree may hit Windows
        # file locks if Chrome's still flushing — retry once.
        for attempt in range(2):
            try:
                shutil.rmtree(tmpdir, ignore_errors=False)
                break
            except OSError:
                if attempt == 0:
                    time.sleep(0.6)
                else:
                    # Last resort: ignore_errors so the function
                    # always returns. Stragglers will get cleaned by
                    # the next cleanup_quarantine_dirs sweep on
                    # dashboard restart.
                    shutil.rmtree(tmpdir, ignore_errors=True)


def _result(status: str,
            duration: float = 0.0,
            exit_code: Optional[int] = None,
            errors: Optional[list[str]] = None,
            warnings: Optional[list[str]] = None,
            log_excerpt: str = "",
            reason: str = "") -> dict:
    """Common result dict shape — keeps all fields present so
    frontend doesn't need defensive defaults."""
    OK_STATUSES = {"loads"}
    return {
        "ok":          status in OK_STATUSES,
        "status":      status,
        "duration":    duration,
        "exit_code":   exit_code,
        "errors":      errors or [],
        "warnings":    warnings or [],
        "log_excerpt": log_excerpt,
        "reason":      reason,
    }


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m ghost_shell.extensions.solo_test "
              "<extension_id_or_path>")
        sys.exit(2)
    target = sys.argv[1]
    if os.path.isdir(target):
        result = test_extension_path(target)
    else:
        result = test_extension(target)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["ok"] else 1)
