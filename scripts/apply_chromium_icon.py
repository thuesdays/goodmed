"""
apply_chromium_icon.py -- Copy Ghost Shell favicon into Chromium source tree.

Run this once before the first Chromium rebuild that should ship our icon.
Subsequent rebuilds don't need this unless you change favicon.svg.

Steps:
  1. Regenerates all icon sizes from dashboard/favicon.svg via make_favicon.py
  2. Copies PNGs/ICO into chrome/app/theme/chromium/ with Chromium's naming
  3. On macOS, builds app.icns using iconutil (if available)
  4. On Linux, copies to the correct theme subdir

Usage:
    python apply_chromium_icon.py --chromium-src F:/projects/chromium/src
    python apply_chromium_icon.py           # auto-detect from env or default

After this runs, rebuild Chromium:
    autoninja -C out/Default chrome
"""
import argparse
import os
import shutil
import subprocess
import sys
import platform


# Favicon size -> Chromium product_logo_* filename mapping.
# Chromium ships these as raw PNG resources bundled into product_logo.ico
# at link time. Missing sizes silently fall back to the next-larger, so
# the set below is the canonical one - losing any entry reduces icon
# crispness on DPI-scaled displays.
CHROMIUM_ICON_MAP = [
    ("favicon-16.png",  "product_logo_16.png"),
    ("favicon-32.png",  "product_logo_32.png"),
    ("favicon-48.png",  "product_logo_48.png"),
    ("favicon-64.png",  "product_logo_64.png"),
    ("favicon-128.png", "product_logo_128.png"),
    ("favicon-256.png", "product_logo_256.png"),
    ("favicon.ico",     "product_logo.ico"),
    # Windows-specific binary icon paths
    ("favicon.ico",     "win/chromium.ico"),
    ("favicon.ico",     "win/chromium_doc.ico"),
    ("favicon.ico",     "win/chromium_pdf.ico"),
]


def find_project_root() -> str:
    """The dir containing dashboard/favicon.svg. Walk up from this file
    until we find it, so the script works from any cwd."""
    here = os.path.abspath(os.path.dirname(__file__))
    for _ in range(5):
        if os.path.exists(os.path.join(here, "dashboard", "favicon.svg")):
            return here
        here = os.path.dirname(here)
    raise RuntimeError(
        "Couldn't locate Ghost Shell project root (no dashboard/favicon.svg found)"
    )


def find_chromium_src(override: str = None) -> str:
    """Chromium source tree. Order: CLI flag -> CHROMIUM_SRC env -> default path."""
    if override:
        return os.path.abspath(override)
    env = os.environ.get("CHROMIUM_SRC")
    if env:
        return os.path.abspath(env)
    # Common defaults per platform
    if platform.system() == "Windows":
        return "F:/projects/chromium/src"
    return os.path.expanduser("~/chromium/src")


def regenerate_favicons(project_root: str) -> None:
    """Invoke dashboard/make_favicon.py to produce the full PNG set."""
    script = os.path.join(project_root, "dashboard", "make_favicon.py")
    if not os.path.exists(script):
        print(f"  [!] make_favicon.py not found at {script} - skipping regen")
        return
    print(f"  > Regenerating icons from favicon.svg...")
    result = subprocess.run(
        [sys.executable, script],
        cwd=os.path.join(project_root, "dashboard"),
    )
    if result.returncode != 0:
        raise SystemExit("make_favicon.py failed - aborting icon copy")


def copy_windows_linux(project_root: str, chromium_src: str) -> int:
    """Copy PNG/ICO files into chrome/app/theme/chromium/. Returns count."""
    theme_dir = os.path.join(
        chromium_src, "chrome", "app", "theme", "chromium"
    )
    if not os.path.isdir(theme_dir):
        raise SystemExit(
            f"Chromium theme dir not found: {theme_dir}\n"
            f"Check --chromium-src or CHROMIUM_SRC env var."
        )

    src_dir = os.path.join(project_root, "dashboard")
    copied = 0
    for src_name, dst_name in CHROMIUM_ICON_MAP:
        src_path = os.path.join(src_dir, src_name)
        dst_path = os.path.join(theme_dir, dst_name)
        if not os.path.exists(src_path):
            print(f"  [!] {src_name} missing - skipping")
            continue
        # Chromium's theme dir is version-controlled; callers typically
        # want to see the diff. We don't back up - git already does.
        shutil.copy2(src_path, dst_path)
        print(f"  [ok] {src_name:20s} -> {dst_name}")
        copied += 1
    return copied


def build_macos_icns(project_root: str, chromium_src: str) -> bool:
    """Build app.icns from favicon PNGs using iconutil (macOS only).
    Returns True on success."""
    if platform.system() != "Darwin":
        return False
    src_dir = os.path.join(project_root, "dashboard")
    iconset_dir = os.path.join(project_root, "_tmp.iconset")
    os.makedirs(iconset_dir, exist_ok=True)

    # macOS iconset naming convention - sizes paired with @2x variants
    icns_map = {
        "favicon-16.png":  "icon_16x16.png",
        "favicon-32.png":  ["icon_16x16@2x.png", "icon_32x32.png"],
        "favicon-64.png":  "icon_32x32@2x.png",
        "favicon-128.png": "icon_128x128.png",
        "favicon-256.png": ["icon_128x128@2x.png", "icon_256x256.png"],
        "favicon-512.png": ["icon_256x256@2x.png", "icon_512x512.png"],
    }
    for src, dsts in icns_map.items():
        if isinstance(dsts, str):
            dsts = [dsts]
        src_path = os.path.join(src_dir, src)
        if not os.path.exists(src_path):
            continue
        for dst in dsts:
            shutil.copy2(src_path, os.path.join(iconset_dir, dst))

    print(f"  > Running iconutil to build app.icns...")
    try:
        subprocess.run(
            ["iconutil", "-c", "icns", iconset_dir],
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"  [!] iconutil failed ({e}) - icns NOT updated")
        shutil.rmtree(iconset_dir, ignore_errors=True)
        return False

    icns_built = os.path.join(project_root, "_tmp.icns")
    mac_dst = os.path.join(
        chromium_src, "chrome", "app", "theme", "chromium", "mac", "app.icns"
    )
    os.makedirs(os.path.dirname(mac_dst), exist_ok=True)
    shutil.copy2(icns_built, mac_dst)
    print(f"  [ok] app.icns -> {mac_dst}")

    shutil.rmtree(iconset_dir, ignore_errors=True)
    os.remove(icns_built)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--chromium-src", help="Path to chromium/src (overrides auto-detect)")
    ap.add_argument("--skip-regen", action="store_true",
                    help="Skip favicon regeneration (use existing PNGs)")
    args = ap.parse_args()

    project_root = find_project_root()
    chromium_src = find_chromium_src(args.chromium_src)
    print(f"Project root : {project_root}")
    print(f"Chromium src : {chromium_src}")
    print()

    if not args.skip_regen:
        regenerate_favicons(project_root)
        print()

    print("> Copying into Chromium theme dir...")
    copied = copy_windows_linux(project_root, chromium_src)
    print(f"  - {copied} files copied\n")

    if platform.system() == "Darwin":
        print("> Building macOS icns...")
        build_macos_icns(project_root, chromium_src)
        print()

    print("[ok] Done. Rebuild Chromium now:")
    print(f"    cd {chromium_src}")
    print(f"    autoninja -C out/Default chrome")
    print()
    print("After the rebuild, right-click chrome.exe -> Properties -> Details")
    print("to confirm the icon changed. If the old icon sticks in Explorer,")
    print("delete %LOCALAPPDATA%\\IconCache.db and restart Windows Explorer.")


if __name__ == "__main__":
    main()
