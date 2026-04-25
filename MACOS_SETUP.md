# Ghost Shell Anty on macOS

On Mac you can run the **dashboard, config editor, proxy diagnostics,
profile management and historical data browser**. The actual monitoring
runs (which open Chrome and search Google) require our custom-patched
Chromium -- and that binary is shipped pre-built only for Windows.

This guide covers the two practical setups:

1. **Dashboard-only on Mac** -- read/edit config, inspect data, run
   proxy tests.
2. **Remote monitoring** -- Mac dashboard drives the Windows build box
   over SSH so clicking **Start** actually triggers a run.
3. (Optional) **Native Mac monitoring** -- build Chromium yourself,
   covered in `MACOS_BUILD.md`.

---

## 1. Dashboard-only setup

```bash
# Project folder
cd ~/ghost-shell
git clone https://github.com/thuesdays/ghost_shell_browser.git .

# Python environment (3.11+)
python3.11 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

### Verify

```bash
python -c "from ghost_shell.core.platform_paths import PLATFORM, PROJECT_ROOT, find_chrome_binary, find_chromedriver; print(\"PLATFORM:    \", PLATFORM); print(\"PROJECT_ROOT:\", PROJECT_ROOT); print(\"Chrome found:\", find_chrome_binary()); print(\"Driver found:\", find_chromedriver())"
```

Expected output on a Mac without a local Chromium build:

```
PLATFORM:     darwin
PROJECT_ROOT: /Users/you/ghost-shell
Chrome found: None          <-- expected, no Mac binary shipped
Driver found: None
```

### Run the dashboard

```bash
python -m ghost_shell dashboard
```

The dashboard auto-opens at `http://127.0.0.1:5000` in your default
browser. The launch-config in `.vscode/launch.json` also wraps this
command if you prefer F5 from VS Code.

**Works on Mac:**

- Every dashboard page (Overview, Profiles, Fingerprint, Session/
  Cookies, Proxy, Domains, Competitors, Behavior, Scripts,
  Accounts & Vault, Runs, Traffic, Scheduler, Logs, Settings)
- Profile creation, fingerprint generation, coherence validation
- Proxy diagnostics + bulk import
- Credential vault (master password, encrypted accounts/wallets/keys)
- Historical reports — runs, competitors, IP stats, traffic
- Cookie snapshot management

**Doesn't work on Mac (no local Chromium):**

- **Start** button on a profile -- raises an error because there is
  no `chrome_mac/Chromium.app/...`
- `python -m ghost_shell monitor` -- same reason
- Fingerprint **Self-test** tab (it launches a real browser)
- Session **Warmup robot** -- needs the real browser to walk sites
- Cookie snapshot **Restore** at next launch -- needs a launch

Everything that does NOT require launching the patched Chromium runs
identically to Windows.

---

## 2. Remote monitoring (recommended)

Run the dashboard from Mac but have monitor runs actually happen on
the Windows box through an SSH tunnel.

### On the Windows box (one-time)

```powershell
# Install OpenSSH server if absent: Settings -> Optional Features
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
```

Open port 22 in the firewall to your local network.

### On Mac

```bash
# Forward Windows :5000 -> Mac :5000
ssh -L 5000:localhost:5000 user@windows-box

# Inside the SSH session:
cd F:/projects/ghost_shell_browser
.venv\Scripts\activate
$env:GHOST_SHELL_NO_BROWSER="1"  # PowerShell on Win — disable auto-open of Edge
python -m ghost_shell dashboard
```

Then on Mac open a local browser at `http://127.0.0.1:5000` -- the
SSH tunnel forwards to the Windows dashboard. Every click (Start /
Rotate IP / Self-test / Run Warmup) runs on the Windows box; the
Chrome window opens there, you watch the dashboard here.

### Alternative: shared SQLite

Sync `ghost_shell.db` between Windows (the actual runner) and Mac (the
analyst seat) via Dropbox / iCloud / `rsync`. The Mac dashboard reads
it just fine. Configuration changes from either side are picked up by
the next dashboard reload.

---

## 3. Platform-specific behaviour in the codebase

Everything OS-aware lives in `ghost_shell/core/platform_paths.py`:

| Behaviour                       | Windows                         | macOS                                            |
|---------------------------------|---------------------------------|--------------------------------------------------|
| Chrome binary (default)         | `chrome_win64/chrome.exe`       | `chrome_mac/Chromium.app/Contents/MacOS/Chromium`|
| Chromedriver                    | `chromedriver.exe`              | `chromedriver`                                   |
| Popen flags (run isolation)     | `CREATE_NEW_PROCESS_GROUP`      | `start_new_session=True`                         |
| SingletonLock (chrome profile)  | regular file                    | symlink                                          |
| Auto-open dashboard browser     | `webbrowser.open()`             | same                                             |
| `PROJECT_ROOT` resolution       | `parent of ghost_shell/`        | same                                             |

The code picks the right path/flag automatically. You don't have to
configure anything per-platform.

---

## 4. Native Mac monitoring (build Chromium yourself)

If you want monitor runs natively on Mac without the SSH bridge:

1. Follow `MACOS_BUILD.md` to build the patched Chromium for arm64
   or x86_64. Takes 1-4 hours on first build.
2. Deploy the result into `chrome_mac/` next to the project.
3. `python -m ghost_shell monitor` will pick up `Chromium.app`
   automatically via `find_chrome_binary()`.

The deploy script strips Gatekeeper quarantine
(`xattr -dr com.apple.quarantine chrome_mac/`) and places
chromedriver alongside.

**Known Mac gotchas if you go this route:**

- **Code signing** -- unsigned builds work locally but Gatekeeper may
  complain the first time. Right-click -> Open to bypass.
- **Apple Silicon vs Intel** -- `target_cpu` in `args.gn` must match
  your host CPU, otherwise Rosetta 2 takes over (~30% slower, looks
  off in fingerprint readings).
- **Screen capture permission** -- macOS asks on first run. Grant in
  System Settings -> Privacy & Security -> Screen Recording.

---

## 5. CLI commands cheat-sheet

After `pip install -r requirements.txt`:

```bash
python -m ghost_shell                  # alias for monitor
python -m ghost_shell monitor          # one ad-monitoring pass
python -m ghost_shell dashboard        # Flask server on :5000
python -m ghost_shell scheduler        # background scheduled-task loop
python -m ghost_shell --help           # usage hint
```

Standalone scripts also work:

```bash
python scripts/diagnose.py             # environment + dependencies + paths check
python tests/test_proxy_live.py        # smoke-test the configured proxy
```

---

## Summary

For most Mac users, the sweet spot is:

- **Dashboard-only** mode for daily use (config tweaks, viewing
  history, vault management)
- **SSH tunnel** to the Windows box when you want to trigger monitor
  runs

The Python layer is fully cross-platform; only the custom Chromium
binary is OS-specific. A native Mac build is doable -- see
`MACOS_BUILD.md` -- but rarely worth the multi-hour setup if you
already have a Windows machine handy.
