# Troubleshooting

## Install / setup

### `GhostShellAntySetup.exe` won't run / SmartScreen blocks

The installer is unsigned at the moment. Click **More info** on the
SmartScreen dialog, then **Run anyway**. Code signing is a planned
addition once we have a budget for the cert.

### "Python 3.11+ required" but I have it

The installer probes via the registry. If you installed Python
manually outside the standard installer (e.g. via `pyenv-win` or
the Microsoft Store), the registry entry might be missing. Either:

- Install the Python.org installer once (it just adds the registry
  entry; doesn't replace your existing Python), or
- Set env var `GHOST_SHELL_PYTHON=C:\path\to\python.exe` before
  running the installer.

### Dashboard doesn't open after install

Check `%LOCALAPPDATA%\GhostShellAnty\logs\dashboard.log`. Common
reasons:

- Port 5000 is taken — set `GHOST_SHELL_PORT=5050` in env, or edit
  the saved port in `runtime.json`.
- Python deps failed to install — re-run `pip install -r
  requirements.txt` from the install folder's venv.
- `chrome_win64\` is missing — the installer's chromium step
  failed. Re-run the installer or run
  `scripts\download_chromium.ps1` manually.

## Profile launches

### "Chrome already running" / SingletonLock error

Each profile has its own user-data-dir, but if a previous Chrome
instance crashed and left a stale `SingletonLock`, the next launch
errors out. Fix:

```powershell
del profiles\<profile_name>\Default\SingletonLock
del profiles\<profile_name>\Default\SingletonCookie
del profiles\<profile_name>\Default\SingletonSocket
```

The dashboard's **Profile detail → Danger zone → Reset locks**
button does this for you.

### Self-test scores 0/13

Almost always means upstream Chrome (not patched Chromium) is
launching. Check:

1. `chrome_win64\chrome.exe` exists and matches the build
   timestamp on the Releases page.
2. `Settings → paths → browser.binary_path` points at it (not at
   your system Chrome).
3. Re-run `scripts\download_chromium.ps1` if in doubt.

### Self-test passes 12/13 — `webdriver=true`

`navigator.webdriver` is hidden by the C++ patches. If you see this
fail, the patches aren't applied — you're running upstream Chrome.
Same fix as above.

### Self-test fails on timezone

The proxy's geo and the FP's timezone disagree. Either:

- Pick a different proxy (one whose geo matches the FP timezone),
  or
- Lock the FP timezone to whatever the proxy reports
  (Fingerprint editor → timezone field → lock icon).

### Profile launches but no cookies are set

Likely a CDP timing issue. Check:

- The `session.pending_restore.<profile>` key in `config_kv` —
  should be **gone** after a successful restore. If it's still
  there, restore failed silently. The Logs page should show
  why.
- For bulk-create auto-inject: check `<profile_user_data_dir>/
  Default/Cookies` exists and is non-empty before launch.

## Extensions

### Extension installs but won't load

Inspect the dashboard's Logs for messages from `extensions.pool`
during the import. Common reasons:

- **Manifest version unsupported** — Chrome 149 doesn't accept
  some old MV2 patterns. Try a newer version of the extension.
- **`_locales/` folder present without `default_locale`** —
  normalizer should fix this on import; if it didn't, file an
  issue with the failing CRX/zip.
- **`__MSG_*` placeholders unresolvable** — manifest references a
  message ID that doesn't exist in any locale. Normalizer
  substitutes a fallback; if you see the original placeholder
  text in Chrome, the substitution failed.

### Toolbar pin doesn't stick

Chrome 149 stores the pin under `extensions.pinned_actions`. We
write all three known keys (`pinned_actions`,
`pinned_extensions`, `toolbar`) every launch. If the pin still
doesn't show, manually pin once via the puzzle-piece menu — the
manual pin survives, and our every-launch re-pin code is
idempotent (won't double-pin or unpin).

### Extension data doesn't persist between launches

The `user_data_dir` got nuked or reset. Per-profile extension
state lives in:

```
profiles/<name>/Default/Local Extension Settings/<id>/
profiles/<name>/Default/IndexedDB/chrome-extension_<id>_0.indexeddb.leveldb/
profiles/<name>/Default/Storage/ext/<id>/
```

If those dirs are getting wiped, something's calling
`shutil.rmtree(profile_dir)` between launches. Most likely the
**Reinstall fresh** flow on the installer — it backs up
`ghost_shell.db` but does delete `profiles/`.

## Proxy

### Proxy diagnostics returns "auth failed"

Check the password in the Proxy library. Currently stored
plaintext in the `proxies` table (legacy — slated to migrate to
the vault). If the password contains special characters, the
URL-encoded form might be required by some providers.

### Proxy shows correct geo in diagnostics, but Self-test reports a different one

WebRTC IP leak. Check `Self-test → WebRTC` — if it reports your
real IP, the proxy isn't being applied to WebRTC. Fix in
`Settings → proxy.force_webrtc_proxy = true` (forces WebRTC
through the proxy via Chrome flags).

## Cookies / sessions

### "Cookie pool inject" picks no donor

The match algorithm requires:

- At least one donor snapshot exists for a profile with overlapping
  tags (first non-system tag = category)
- Donor snapshot is < 30 days old by default (configurable in
  `Settings → session.pool_max_age_days`)

If you have donor snapshots but the matcher returns zero, check the
profile's tags — if it has none, the matcher has no signal to
work with.

### Cookies are restored but the site logs me out anyway

Common reasons (from
[Cookie Pool](Cookie-Pool.md#when-not-to-use-pool-injection)):

- Site fingerprints heavily — donor and destination FPs differ,
  triggers re-login.
- CSRF tokens are session-bound and not in the cookie jar.
- Mismatched proxy geo — donor was on US proxy, you're on EU,
  triggers a security challenge.

For these sites, log in fresh; cookie inject is for low-stakes
session continuity (consents, language prefs), not for full
session takeover.

## DB / state

### "no such column" errors after upgrade

Idempotent migrations should prevent this — `_ensure_column()`
checks before adding. If you somehow hit it:

```powershell
python -c "from ghost_shell.db import get_db; get_db().init_schema()"
```

This re-runs `init_schema()`, which is safe to call multiple
times.

### `ghost_shell.db` is locked

SQLite WAL mode allows multiple readers + one writer. If you
opened the DB in a separate `sqlite3` process and held a
write transaction, the dashboard will block until that frees.
Quit the other process.

### Want to nuke everything and start fresh

```powershell
# stops dashboard if running, then:
del ghost_shell.db
rmdir /s /q profiles
rmdir /s /q logs
python -m ghost_shell dashboard
```

The dashboard recreates the DB on first launch.

## Still stuck?

[Open an issue](https://github.com/thuesdays/ghost_shell_browser/issues)
with:

- Dashboard version (Settings → About)
- Chrome / Chromium version (Logs → first launch line)
- Steps to reproduce
- The relevant chunk of `logs/dashboard.log` or `logs/run-<n>.log`

The Logs page has a **Download** button that bundles everything
maintainers need.
