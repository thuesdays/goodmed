#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# build-ghost-shell.sh — macOS / Linux full build + deploy
#
# Runs autoninja against our Ghost Shell Chromium build, then copies
# the output into the project's chrome_mac/ (or chrome_linux/) folder.
#
# Usage:
#   ./build-ghost-shell.sh                 # build + deploy
#   ./build-ghost-shell.sh --skip-build    # deploy only (binaries already built)
#   ./build-ghost-shell.sh --skip-deploy   # build only, no copy
#
# Environment variables:
#   CHROMIUM_SRC  = path to chromium/src      (default: ../chromium/src)
#   BUILD_DIR     = relative build dir name   (default: out/GhostShell)
# ═════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Parse flags ─────────────────────────────────────────────────────
SKIP_BUILD=0
SKIP_DEPLOY=0
for arg in "$@"; do
    case "$arg" in
        --skip-build)  SKIP_BUILD=1  ;;
        --skip-deploy) SKIP_DEPLOY=1 ;;
        -h|--help)
            head -30 "$0" | tail -20 | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "[ERROR] Unknown flag: $arg"
            exit 1
            ;;
    esac
done

# ── Detect platform ─────────────────────────────────────────────────
case "$(uname -s)" in
    Darwin*)  PLATFORM="mac"   ;;
    Linux*)   PLATFORM="linux" ;;
    *)
        echo "[ERROR] Unsupported platform: $(uname -s)"
        echo "        Use build-ghost-shell.bat on Windows."
        exit 1
        ;;
esac

# ── Config ──────────────────────────────────────────────────────────
CHROMIUM_SRC="${CHROMIUM_SRC:-../chromium/src}"
BUILD_DIR="${BUILD_DIR:-out/GhostShell}"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST_DIR="chrome_${PLATFORM}"

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║  Ghost Shell — full build pipeline (${PLATFORM})"
echo "╚════════════════════════════════════════════════════════════════╝"
echo "  Chromium src:  ${CHROMIUM_SRC}"
echo "  Build dir:     ${BUILD_DIR}"
echo "  Deploy dir:    ${PROJECT_DIR}/${DEST_DIR}"
echo

# ── Resolve absolute Chromium src path ──────────────────────────────
if [[ ! -d "${CHROMIUM_SRC}" ]]; then
    echo "[ERROR] Chromium source not found: ${CHROMIUM_SRC}"
    echo "        Set CHROMIUM_SRC env var to point at chromium/src"
    exit 1
fi
CHROMIUM_SRC="$(cd "${CHROMIUM_SRC}" && pwd)"

# ── autoninja build ─────────────────────────────────────────────────
if [[ "$SKIP_BUILD" == "1" ]]; then
    echo "[1/3] Build skipped (--skip-build)"
else
    echo "[1/3] Building chrome + chromedriver (this can take 1-4 hours on first run)"
    pushd "${CHROMIUM_SRC}" > /dev/null
    if ! command -v autoninja > /dev/null; then
        echo "[ERROR] autoninja not in PATH. Set up depot_tools first."
        exit 1
    fi
    autoninja -C "${BUILD_DIR}" chrome chromedriver
    popd > /dev/null
    echo
fi

# ── Print version ───────────────────────────────────────────────────
VERSION_FILE="${CHROMIUM_SRC}/chrome/VERSION"
if [[ -f "${VERSION_FILE}" ]]; then
    MAJOR=$(grep '^MAJOR=' "$VERSION_FILE" | cut -d'=' -f2)
    MINOR=$(grep '^MINOR=' "$VERSION_FILE" | cut -d'=' -f2)
    BUILD=$(grep '^BUILD=' "$VERSION_FILE" | cut -d'=' -f2)
    PATCH=$(grep '^PATCH=' "$VERSION_FILE" | cut -d'=' -f2)
    echo "[2/3] Chromium version: ${MAJOR}.${MINOR}.${BUILD}.${PATCH}"
else
    echo "[2/3] (chrome/VERSION not found, skipping version check)"
fi
echo

# ── Deploy ──────────────────────────────────────────────────────────
if [[ "$SKIP_DEPLOY" == "1" ]]; then
    echo "[3/3] Deploy skipped (--skip-deploy)"
else
    echo "[3/3] Deploying to ${PROJECT_DIR}/${DEST_DIR}"
    CHROMIUM_SRC="${CHROMIUM_SRC}" BUILD_DIR="${BUILD_DIR}" \
        "${PROJECT_DIR}/deploy-ghost-shell-flat.sh"
fi

echo
echo "════════════════════════════════════════════════════════════════"
echo " ✓ Build pipeline complete"
echo "════════════════════════════════════════════════════════════════"
echo
echo "   Verify:"
echo "     python platform_paths.py"
echo "     python test_chromedriver.py"
echo
