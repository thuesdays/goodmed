#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# deploy-ghost-shell-flat.sh — macOS/Linux deploy for Ghost Shell
#
# Copies the freshly-built Chromium + chromedriver from the Chromium
# source tree into ./chrome_mac (or ./chrome_linux) inside the Ghost
# Shell project, so everything our Python code expects is in one place.
#
# Usage:
#   ./deploy-ghost-shell-flat.sh                 # uses defaults
#   CHROMIUM_SRC=/path/to/chromium/src ./deploy-ghost-shell-flat.sh
#
# Environment variables:
#   CHROMIUM_SRC   path to chromium/src   (default: ../chromium/src)
#   BUILD_DIR      build dir name         (default: out/GhostShell)
#   DEST_DIR       deploy destination     (default: chrome_mac / chrome_linux)
# ═════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Detect platform ─────────────────────────────────────────────────
case "$(uname -s)" in
    Darwin*)  PLATFORM="mac"   ;;
    Linux*)   PLATFORM="linux" ;;
    *)
        echo "[ERROR] Unsupported platform: $(uname -s)"
        echo "Use deploy-ghost-shell-flat.bat on Windows."
        exit 1 ;;
esac

# ── Config ──────────────────────────────────────────────────────────
CHROMIUM_SRC="${CHROMIUM_SRC:-../chromium/src}"
BUILD_DIR="${BUILD_DIR:-out/GhostShell}"
DEST_DIR="${DEST_DIR:-chrome_${PLATFORM}}"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

BUILD_PATH="${CHROMIUM_SRC}/${BUILD_DIR}"
DEST_PATH="${PROJECT_DIR}/${DEST_DIR}"

echo "════════════════════════════════════════════════════════════════"
echo " Ghost Shell deploy (${PLATFORM})"
echo "════════════════════════════════════════════════════════════════"
echo "  Source: ${BUILD_PATH}"
echo "  Dest:   ${DEST_PATH}"
echo

# ── Sanity check ────────────────────────────────────────────────────
if [[ ! -d "${BUILD_PATH}" ]]; then
    echo "[ERROR] Build directory not found: ${BUILD_PATH}"
    echo "        Set CHROMIUM_SRC env var to point at your chromium/src"
    exit 1
fi

# ── Wipe old deploy (preserve logs) ─────────────────────────────────
if [[ -d "${DEST_PATH}" ]]; then
    echo "[1/4] Cleaning old deploy..."
    find "${DEST_PATH}" -mindepth 1 -not -name "*.log" -delete 2>/dev/null || true
else
    echo "[1/4] Creating dest directory..."
    mkdir -p "${DEST_PATH}"
fi

# ── Copy Chromium ───────────────────────────────────────────────────
if [[ "$PLATFORM" == "mac" ]]; then
    # macOS: Chromium is bundled as Chromium.app — copy whole bundle
    APP_BUNDLE="${BUILD_PATH}/Chromium.app"
    if [[ ! -d "${APP_BUNDLE}" ]]; then
        echo "[ERROR] Chromium.app not found at ${APP_BUNDLE}"
        echo "        Did you run: autoninja -C out/GhostShell chrome ?"
        exit 1
    fi
    echo "[2/4] Copying Chromium.app (this takes a moment)..."
    cp -R "${APP_BUNDLE}" "${DEST_PATH}/"
    # Print the real binary path we just deployed
    REAL_BINARY="${DEST_PATH}/Chromium.app/Contents/MacOS/Chromium"
else
    # Linux: flat layout — chrome binary + libraries
    echo "[2/4] Copying Linux Chromium..."
    cp "${BUILD_PATH}/chrome" "${DEST_PATH}/"

    # Copy shared dependencies — we need all the .so and resource files
    for f in chrome_100_percent.pak chrome_200_percent.pak \
             resources.pak icudtl.dat v8_context_snapshot.bin \
             snapshot_blob.bin chrome_crashpad_handler \
             libEGL.so libGLESv2.so libvk_swiftshader.so \
             libVkICD_mock_icd.so vk_swiftshader_icd.json; do
        if [[ -f "${BUILD_PATH}/${f}" ]]; then
            cp "${BUILD_PATH}/${f}" "${DEST_PATH}/"
        fi
    done

    # Locales
    if [[ -d "${BUILD_PATH}/locales" ]]; then
        cp -R "${BUILD_PATH}/locales" "${DEST_PATH}/"
    fi

    chmod +x "${DEST_PATH}/chrome"
    REAL_BINARY="${DEST_PATH}/chrome"
fi

# ── chromedriver ────────────────────────────────────────────────────
echo "[3/4] Copying chromedriver..."
CHROMEDRIVER_SRC="${BUILD_PATH}/chromedriver"
if [[ -f "${CHROMEDRIVER_SRC}" ]]; then
    cp "${CHROMEDRIVER_SRC}" "${DEST_PATH}/"
    chmod +x "${DEST_PATH}/chromedriver"
else
    echo "[WARN] chromedriver not found at ${CHROMEDRIVER_SRC}"
    echo "       Build it with: autoninja -C ${BUILD_DIR} chromedriver"
fi

# ── macOS: remove quarantine attr so Gatekeeper doesn't block ───────
if [[ "$PLATFORM" == "mac" ]]; then
    echo "[4/4] Removing quarantine attributes (macOS)..."
    xattr -dr com.apple.quarantine "${DEST_PATH}" 2>/dev/null || true
fi

echo
echo "════════════════════════════════════════════════════════════════"
echo " ✓ Deploy complete"
echo "────────────────────────────────────────────────────────────────"
echo "   Chrome binary:  ${REAL_BINARY}"
if [[ -f "${DEST_PATH}/chromedriver" ]]; then
    echo "   Chromedriver:   ${DEST_PATH}/chromedriver"
fi
echo
echo "   Verify:"
echo "     python platform_paths.py"
echo "     python test_chromedriver.py"
echo "════════════════════════════════════════════════════════════════"
