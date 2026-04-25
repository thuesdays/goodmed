# Building Ghost Shell Chromium on macOS

Exact step-by-step instructions for building our patched Chromium on
macOS. After this, the Python project works end-to-end on Mac — same
**▶ Start**, same monitoring runs, same self-check as on Windows.

Takes ~1-4 hours on first build depending on your Mac. Incremental
rebuilds (after editing one `.cc` file) take ~1-2 minutes.

---

## Prerequisites

**Disk**: 150 GB free (Chromium source ~40 GB + build output ~60 GB
+ overhead).

**RAM**: 16 GB minimum. 32 GB strongly recommended.

**Xcode Command Line Tools**:

```bash
xcode-select --install
```

Full Xcode (from App Store) is **not** required unless you'll be
developing on the patches yourself.

**Python 3.11+**:

```bash
brew install python@3.11 git
```

---

## 1. Install depot_tools

Google's build tooling for Chromium. Includes `gn`, `autoninja`, `gclient`.

```bash
cd ~
git clone https://chromium.googlesource.com/chromium/tools/depot_tools.git
```

Add to your shell profile. For zsh (default on modern macOS):

```bash
echo 'export PATH="$HOME/depot_tools:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

Verify:

```bash
which autoninja
# → /Users/you/depot_tools/autoninja
```

---

## 2. Fetch Chromium source

```bash
mkdir -p ~/chromium && cd ~/chromium
fetch --nohooks chromium
```

This downloads ~40 GB. Expect 30-90 minutes depending on your
connection. When done:

```bash
cd src
gclient runhooks
```

This resolves dependencies. Another ~5-10 minutes.

---

## 3. Apply Ghost Shell patches

Copy our patched files from the project into Chromium source.  Assuming
the Ghost Shell project is at `~/ghost-shell/`:

```bash
SRC=~/chromium/src

# Core patch files (new, not modifying existing)
cp ~/ghost-shell/ghost_shell_config.h  $SRC/third_party/blink/renderer/platform/
cp ~/ghost-shell/ghost_shell_config.cc $SRC/third_party/blink/renderer/platform/
```

Then **~12 edits in existing Chromium files** — Navigator hooks, Screen
hooks, Canvas, WebGL, Audio, Timezone, command line registration.
**Full list with exact diffs is in `CHROMIUM_PATCHES.md`** in the project
root. Apply each diff carefully using your favourite text editor, or:

```bash
# If you maintain them as .patch files:
cd $SRC
git apply ~/ghost-shell/chromium-patches/*.patch
```

Verify the additions compile by running `gn check`:

```bash
cd $SRC
gn check out/GhostShell 2>/dev/null || true  # ok if out/ not generated yet
```

---

## 4. Configure build args

Create `out/GhostShell/args.gn`:

```bash
mkdir -p ~/chromium/src/out/GhostShell

cat > ~/chromium/src/out/GhostShell/args.gn <<'EOF'
# Release build, no debug info — smallest binary, fastest runtime
is_debug           = false
symbol_level       = 0
is_component_build = false

# macOS target
target_os  = "mac"
target_cpu = "arm64"   # change to "x64" for Intel Macs

# Less strict compile checks — helps avoid unrelated warnings-as-errors
treat_warnings_as_errors = false
EOF
```

**CRITICAL**: `target_cpu` must match your actual Mac CPU.

- Apple Silicon (M1/M2/M3/M4) → `arm64`
- Intel Mac → `x64`

Check yours:

```bash
uname -m
# arm64 = Apple Silicon
# x86_64 = Intel
```

Running an `arm64` binary on an Intel Mac (or vice-versa) works via
Rosetta 2 but is ~30% slower and leaks architecture info in the
fingerprint. Match it to your host.

Generate the build config:

```bash
cd ~/chromium/src
gn gen out/GhostShell
```

---

## 5. First build (slow)

```bash
cd ~/chromium/src
autoninja -C out/GhostShell chrome chromedriver
```

Expected time:

| Machine               | First build time |
|-----------------------|------------------|
| M3 Max / M2 Ultra     | 45-90 min        |
| M2 Pro / M1 Pro       | 1.5-2.5 hrs      |
| M1 / base M2 / M3     | 2-3 hrs          |
| Intel i7 (2020)       | 3-4 hrs          |

You can interrupt with Ctrl+C and resume — autoninja caches
everything.

Incremental rebuilds after editing one C++ file are **1-2 minutes**.

**If it fails**: common issues

- "No space left on device" → you hit the 100+ GB mark. Clear
  `~/chromium/src/out/GhostShell/obj/` and use bigger disk.
- "warning treated as error" → already disabled in our args.gn, but if
  you see it, add `treat_warnings_as_errors = false` to args.gn and
  re-run `gn gen`.
- Code-signing complaints → ignore, we build unsigned.

---

## 6. Deploy to the project

From the Ghost Shell project folder:

```bash
cd ~/ghost-shell
CHROMIUM_SRC=~/chromium/src ./deploy-ghost-shell-flat.sh
```

This copies the entire `Chromium.app` bundle plus `chromedriver` into
`chrome_mac/`, and strips the macOS quarantine attribute so Gatekeeper
doesn't block it.

Layout after deploy:

```
chrome_mac/
├── Chromium.app/
│   └── Contents/
│       ├── Frameworks/
│       ├── MacOS/
│       │   └── Chromium        ← the actual binary
│       ├── Resources/
│       └── Info.plist
└── chromedriver                ← matching driver
```

---

## 7. Verify everything works

```bash
cd ~/ghost-shell
source .venv/bin/activate

# 1. Platform detection finds the binaries
python platform_paths.py
# → Platform:         darwin
# → Chrome found:     /Users/you/ghost-shell/chrome_mac/Chromium.app/Contents/MacOS/Chromium
# → Driver found:     /Users/you/ghost-shell/chrome_mac/chromedriver

# 2. Smoke test — raw selenium + our chrome
python test_chromedriver.py
# → Opens Chrome, loads google.com, prints title, closes. No errors.

# 3. Full run
python dashboard_server.py
# → Dashboard opens, click ▶ Start
```

If **▶ Start** produces 13/13 self-check and real search results — you're
fully up on Mac.

---

## Troubleshooting

### "Chromium.app cannot be opened — unverified developer"

Gatekeeper. The deploy script strips quarantine, but if you built
without deploying (or the attribute got re-added):

```bash
xattr -dr com.apple.quarantine chrome_mac/
```

Or right-click `Chromium.app` in Finder → Open → Open.

### `chromedriver: Permission denied`

```bash
chmod +x chrome_mac/chromedriver
xattr -d com.apple.quarantine chrome_mac/chromedriver
```

### "The application cannot be opened because of a problem"

Usually means architecture mismatch (`arm64` binary on Intel Mac or
vice-versa). Verify:

```bash
file chrome_mac/Chromium.app/Contents/MacOS/Chromium
# Should say "arm64" on Apple Silicon or "x86_64" on Intel
uname -m    # Your actual CPU
```

If mismatched, rebuild with the right `target_cpu` in `args.gn`.

### Screen Recording permission

macOS asks on first launch. Grant in
**System Settings → Privacy & Security → Screen Recording**. Chromium
needs this for `getDisplayMedia()` and some internal compositing
checks.

### `autoninja: command not found` when called from the build script

The build script needs `depot_tools` in the shell's PATH. If it's only
in your interactive shell (`~/.zshrc`), pass it explicitly:

```bash
PATH="$HOME/depot_tools:$PATH" ./build-ghost-shell.sh
```

### "I already had a Chromium build, why do I need another?"

Our stealth patches live in Chromium source. Upstream Chromium /
Chrome / any antidetect browser you downloaded won't have them —
JS can detect the absence and flag you. Build once, use forever (1-2
min per incremental rebuild after code changes).

---

## Incremental workflow after first build

After editing `ghost_shell_config.cc` (or any other patched file):

```bash
# Copy the edited file back into Chromium source
cp ~/ghost-shell/ghost_shell_config.cc \
   ~/chromium/src/third_party/blink/renderer/platform/

# Touch timestamp (Copy-Item may preserve mtime → ninja thinks nothing changed)
touch ~/chromium/src/third_party/blink/renderer/platform/ghost_shell_config.cc

# Rebuild + deploy
cd ~/ghost-shell
./build-ghost-shell.sh
```

Or just deploy if you already built:

```bash
./build-ghost-shell.sh --skip-build
```

---

## Summary

Once you've done this once, Ghost Shell works identically on Mac and
Windows. The Python layer picks the right binary automatically via
`platform_paths.py`:

| Platform | `browser.binary_path` default                             |
|----------|-----------------------------------------------------------|
| Windows  | `chrome_win64/chrome.exe`                                 |
| macOS    | `chrome_mac/Chromium.app/Contents/MacOS/Chromium`         |
| Linux    | `chrome_linux/chrome`                                     |

No per-platform config. Same dashboard. Same `main.py`. Same everything.
