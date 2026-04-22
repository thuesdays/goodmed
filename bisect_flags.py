"""
bisect_flags.py — Find which Chrome flag causes the "crashed" error.

Starts from minimal working set (smoke test level) and adds main.py's
flags one by one. Prints result of each launch. Stops as soon as one
of them crashes chrome.

Run:
    python bisect_flags.py
"""

import os
import sys
import time
import tempfile
import logging
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

logging.basicConfig(level=logging.WARNING)  # quiet selenium noise

from platform_paths import find_chrome_binary, find_chromedriver

CHROME_EXE   = find_chrome_binary()
CHROMEDRIVER = find_chromedriver()
USER_DATA_DIR = os.path.abspath("profiles/profile_01")

if not CHROME_EXE or not CHROMEDRIVER:
    print("[ERROR] Chrome or chromedriver not found for your platform.")
    print("        Run the deploy script first.")
    sys.exit(1)

# Flags from main.py, in order of suspicion (most to least likely culprit)
EXTRA_FLAGS = [
    f"--user-data-dir={USER_DATA_DIR}",       # proved it works manually
    "--disable-notifications",
    "--disable-infobars",
    "--disable-popup-blocking",
    "--no-default-browser-check",
    "--start-maximized",
    "--accept-lang=uk-UA,uk;q=0.9,ru;q=0.8,en-US;q=0.7,en;q=0.6",
    "--window-size=1920,1032",
]


def try_launch(label: str, flags: list) -> bool:
    """Attempt to launch chrome with the given flags. Returns True if OK."""
    print(f"\n[TEST] {label}")
    print(f"       flags: {flags}")

    options = Options()
    options.binary_location = CHROME_EXE
    for f in flags:
        options.add_argument(f)

    # Always-on base flags
    options.add_argument("--no-sandbox")
    options.add_argument("--no-first-run")
    options.add_argument("--disable-crash-reporter")
    options.add_argument("--disable-breakpad")

    service = Service(executable_path=CHROMEDRIVER)
    try:
        driver = webdriver.Chrome(service=service, options=options)
        print(f"       ✓ OK — title: {driver.title or '(empty)'}")
        time.sleep(1)
        driver.quit()
        return True
    except Exception as e:
        err = str(e).split("\n")[0][:140]
        print(f"       ✗ FAIL — {err}")
        return False


def main():
    print("=" * 70)
    print(" Chrome flag bisect")
    print("=" * 70)
    print(f"  chrome.exe       : {CHROME_EXE}")
    print(f"  chromedriver.exe : {CHROMEDRIVER}")
    print(f"  user-data-dir    : {USER_DATA_DIR}")
    print("=" * 70)

    # 1. Baseline — like smoke test (fresh user-data-dir)
    tmp_dir = tempfile.mkdtemp(prefix="bisect-baseline-")
    ok = try_launch("baseline (fresh user-data-dir, no extras)", [
        f"--user-data-dir={tmp_dir}",
    ])
    if not ok:
        print("\n[ABORT] Even baseline fails — something fundamental is broken.")
        return

    # 2. Use existing profile_01 without extra flags
    ok = try_launch("existing profile_01, no extras", [
        f"--user-data-dir={USER_DATA_DIR}",
    ])
    if not ok:
        print("\n[FOUND] Just using profile_01 crashes chrome (even though "
              "manual launch works). The profile dir content is incompatible "
              "with selenium-launched chrome.")
        return

    # 3. Add flags incrementally
    cumulative = [f"--user-data-dir={USER_DATA_DIR}"]
    flags_to_test = [f for f in EXTRA_FLAGS if not f.startswith("--user-data-dir")]

    for flag in flags_to_test:
        cumulative_copy = cumulative + [flag]
        ok = try_launch(f"+ {flag}", cumulative_copy)
        if not ok:
            print(f"\n[FOUND] The flag that breaks chrome: {flag}")
            print(f"        (combined with: {cumulative})")
            return
        cumulative.append(flag)

    # 4. Add proxy (assuming port 51006 is available; adjust if not)
    # Skipped because proxy requires forwarder running.

    print("\n" + "=" * 70)
    print(" All tested flags OK — problem is in a flag we didn't test yet")
    print(" (proxy-server, ghost-shell-payload, disable-hang-monitor, etc.)")
    print("=" * 70)


if __name__ == "__main__":
    main()
