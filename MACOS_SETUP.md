# Ghost Shell on macOS

On Mac you can run the **dashboard, config editor, proxy diagnostics,
profile management and historical data browser**. The actual monitoring
runs (which open Chrome and search Google) require our custom patched
Chromium — and that binary is built only on Windows.

This guide covers the two practical setups:

1. **Dashboard-only on Mac** — read/edit config, inspect data, run
   proxy tests.
2. **Remote monitoring** — Mac dashboard drives the Windows build box
   over SSH, so clicking **▶ Start** actually triggers a run.

---

## 1. Dashboard-only setup

```bash
# Project folder
cd ~/ghost-shell
git clone … .            # or rsync from your Windows box

# Python environment
python3.11 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

### Verify

```bash
python platform_paths.py
```

Expected output:

```
Platform:         darwin
exe_ext():        ''
Default subdir:   chrome_mac
Chrome found:     None          ← expected, no build here
Driver found:     None
```

### Run the dashboard

```bash
python dashboard_server.py
```

The dashboard auto-opens at `http://127.0.0.1:5000` in your default
browser.

**Works on Mac:**

- All dashboard pages (Overview, Profiles, Proxy, Runs, Scheduler, Logs,
  Competitors, Behavior, Actions, Search)
- **✨ Create profile** and **🔮 Preview fingerprint**
- **🎲 Regenerate fingerprint** for existing profiles
- **🧪 Rotation test** — uses plain `requests` through your proxy,
  doesn't need Chrome
- **🛰 Proxy diagnostics** — current IP, geo-match, timezone
- **⚡ Rotate IP**
- Historical reports — runs, competitors, IP stats, per-profile health

**Doesn't work on Mac:**

- **▶ Start** button → you'll see an error because `chrome_mac/Chromium.app/…`
  doesn't exist.
- `python main.py` — same reason.

---

## 2. Remote monitoring (recommended)

Run the dashboard from Mac but have runs actually happen on the
Windows box through an SSH tunnel.

### On the Windows box

```powershell
# One-time: install OpenSSH server if not already
# Settings → System → Optional Features → add "OpenSSH Server"
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
```

Make sure firewall allows port 22 from your local network.

### On Mac

```bash
ssh -L 5000:localhost:5000 user@windows-box
# Now in the ssh session:
cd F:/projects/goodmedika
.venv\Scripts\activate
python dashboard_server.py
```

Dashboard auto-opens a browser... on the Windows side. Ignore that
window. On the Mac, open your own browser:

```
http://127.0.0.1:5000
```

The SSH `-L` tunnel forwards it from your Mac to the Windows dashboard.
Every click — **▶ Start**, **⚡ Rotate IP**, **🧪 Rotation test**,
**🎲 Regenerate** — executes on the Windows box. The Chrome window
opens there; you see the dashboard here.

**Tip**: disable auto-open on the remote side so it doesn't keep
launching Edge:

```powershell
$env:GHOST_SHELL_NO_BROWSER="1"
python dashboard_server.py
```

### Alternative: shared SQLite DB

Sync `ghost_shell.db` between Windows (actually runs monitors) and Mac
(just reads stats / configures) via Dropbox / iCloud / rsync. The Mac
dashboard will happily show historical data and let you edit config —
you just run the actual monitor on Windows.

---

## 3. Platform-specific behaviour in the codebase

All Python scripts use `platform_paths.py` for OS detection:

| Behaviour                       | Windows                         | macOS                                            |
|---------------------------------|---------------------------------|--------------------------------------------------|
| Chrome binary (default)         | `chrome_win64/chrome.exe`       | `chrome_mac/Chromium.app/Contents/MacOS/Chromium`|
| Chromedriver                    | `chromedriver.exe`              | `chromedriver`                                   |
| Popen flags                     | `CREATE_NEW_PROCESS_GROUP`      | `start_new_session=True`                         |
| SingletonLock (chrome profile)  | regular file                    | symlink                                          |
| Auto-open dashboard             | `webbrowser.open()`             | same                                             |

The code picks the right path/flag automatically. You don't have to
configure anything per-platform.

---

## 4. If you *do* want to build Chromium on Mac later

When/if you want to run monitors directly on Mac:

```bash
# Requirements
xcode-select --install
brew install python@3.11

# depot_tools
git clone https://chromium.googlesource.com/chromium/tools/depot_tools.git
export PATH="$PWD/depot_tools:$PATH"   # also put in ~/.zshrc

# Chromium source
mkdir ~/chromium && cd ~/chromium
fetch --nohooks chromium
cd src
./build/install-build-deps.sh
gclient runhooks

# Apply the same Ghost Shell C++ patches as on Windows
cp ~/ghost-shell/ghost_shell_config.{h,cc} \
   third_party/blink/renderer/platform/
# plus the ~12 edits listed in CHROMIUM_PATCHES.md

# Build config
cat > out/GhostShell/args.gn <<'EOF'
is_debug = false
symbol_level = 0
is_component_build = false
target_os = "mac"
target_cpu = "arm64"    # or "x64" for Intel Macs
treat_warnings_as_errors = false
EOF

gn gen out/GhostShell
autoninja -C out/GhostShell chrome chromedriver    # 1-4 hours

# Deploy
cd ~/ghost-shell
CHROMIUM_SRC=~/chromium/src ./deploy-ghost-shell-flat.sh
```

The deploy script copies `Chromium.app` whole, strips the quarantine
attribute (`xattr -dr com.apple.quarantine`), and places chromedriver
alongside. After that `python main.py` works identically to Windows.

**Known Mac gotchas if you go this route:**

- **Code signing** — unsigned builds work for local use but Gatekeeper
  may complain the first time. Right-click → Open to bypass, or
  `xattr -dr com.apple.quarantine chrome_mac/`.
- **Apple Silicon vs Intel** — `target_cpu` must match your host CPU,
  otherwise Rosetta 2 takes over (~30% slower, looks weird in
  fingerprint).
- **Screen capture permission** — macOS asks on first run. Grant in
  System Settings → Privacy & Security → Screen Recording.

---

## Summary

For most Mac users, the sweet spot is:

- **Dashboard-only** mode for daily use (config tweaks, viewing history)
- **SSH tunnel** to the Windows box when you want to trigger monitor runs

The Python layer is fully cross-platform; only the custom Chromium
binary is OS-specific.
