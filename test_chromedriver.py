"""
test_chromedriver.py — Minimal test: does chromedriver + chrome launch?

This cuts out all the Ghost Shell layers (uc, payload, watchdog, etc.)
and tries to launch chrome via the raw chromedriver protocol.

Run:
    python test_chromedriver.py

Expected: a Chrome window opens, navigates to google.com, prints title,
then closes after 5 seconds. No errors.

If this fails, the problem is in chromedriver/chrome setup — not in
the Ghost Shell code.
"""

import os
import sys
import time
import logging
import tempfile
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from platform_paths import find_chrome_binary, find_chromedriver, PLATFORM

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# Resolved per-platform (chrome_win64/ on Windows, chrome_mac/Chromium.app/… on Mac, etc.)
CHROME_EXE   = find_chrome_binary()
CHROMEDRIVER = find_chromedriver()

print("=" * 60)
print(" Chromedriver + Chrome smoke test")
print("=" * 60)
print(f"  Platform:     {PLATFORM}")

if not CHROME_EXE:
    print(f"[ERROR] Chrome binary not found for platform {PLATFORM}.")
    print(f"        Windows expected: chrome_win64/chrome.exe")
    print(f"        macOS expected:   chrome_mac/Chromium.app/Contents/MacOS/Chromium")
    print(f"        Linux expected:   chrome_linux/chrome")
    print(f"        Did you run the deploy script?")
    sys.exit(1)

if not CHROMEDRIVER:
    print(f"[ERROR] chromedriver not found next to {CHROME_EXE}")
    print(f"        Build: autoninja -C out/GhostShell chromedriver")
    sys.exit(1)

# Check binaries exist
for p in (CHROME_EXE, CHROMEDRIVER):
    size_mb = os.path.getsize(p) / 1024 / 1024
    print(f"  OK  {p}  ({size_mb:.1f} MB)")
print()

# Build minimal options
options = Options()
options.binary_location = CHROME_EXE
options.add_argument("--no-sandbox")
options.add_argument("--no-first-run")
options.add_argument("--disable-crash-reporter")
options.add_argument("--disable-breakpad")
options.add_argument("--window-position=100,100")
options.add_argument("--window-size=1280,800")
# DO NOT add --headless — we want to see the window

# Give a unique user data dir to avoid any "already in use" issues
import tempfile
user_data_dir = tempfile.mkdtemp(prefix="chr-smoke-")
options.add_argument(f"--user-data-dir={user_data_dir}")
print(f"  User data dir: {user_data_dir}")
print()

print("[1/4] Starting chromedriver...")
service = Service(executable_path=CHROMEDRIVER,
                  log_output=os.path.join(os.getcwd(), "chromedriver.log"))
print("      (chromedriver log will be written to chromedriver.log)")

print("[2/4] Starting chrome via webdriver...")
try:
    driver = webdriver.Chrome(service=service, options=options)
    print("      OK — chrome started, CDP connected")
except Exception as e:
    print(f"\n[FAIL] Could not start chrome:")
    print(f"       {type(e).__name__}: {e}")
    print()
    print("Check chromedriver.log for details.")
    sys.exit(1)

print("[3/4] Navigating to https://www.google.com ...")
try:
    driver.get("https://www.google.com")
    print(f"      Title: {driver.title}")
    print(f"      URL:   {driver.current_url}")
except Exception as e:
    print(f"      [WARN] {e}")

print("[4/4] Waiting 10 seconds so you can see the window ...")
print("      (you SHOULD see a Chrome window with Google loaded)")
time.sleep(10)

driver.quit()
print()
print("=" * 60)
print(" Test complete.")
print(" If a window appeared and Google loaded — chromedriver works!")
print(" If it hung / crashed — check chromedriver.log")
print("=" * 60)
