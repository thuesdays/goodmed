# Quick Start

From a fresh install to your first run, in about ten minutes.

## 1. Install

End users on Windows: download
[`GhostShellAntySetup.exe`](https://github.com/thuesdays/ghost_shell_browser/releases)
and run it. The installer ships the patched Chromium runtime, a
Python venv, and every dependency — no git or pip needed.

The dashboard auto-opens at `http://127.0.0.1:5000` after install.

Source / dev install: see the
[README](https://github.com/thuesdays/ghost_shell_browser#develop-from-source).

## 2. First profile

Sidebar → **Profiles** → **+ New profile**. The form needs:

- **Name** — anything; `profile_01`, `client_acme`, `qa_ipad_dev`
- **Template** — picks a coherent device fingerprint (UA, GPU, fonts,
  timezone all from one device)
- **Proxy** (optional but recommended) — pick from your proxy library
  or leave blank to use your real IP
- **Tags** (optional) — used by Cookie pool category-matching and the
  Profiles filter

Save. The profile shows up in the table with status `idle`.

## 3. Verify the fingerprint

Click the profile row → **Fingerprint** tab → **Self-test**. This
launches a real browser, runs a battery of detection checks, and
returns a coherence score plus a per-field configured-vs-actual
comparison.

A healthy profile scores ≥80. The bad signals to watch for:

- `webdriver=true` — patched Chromium not in use; you're running
  upstream Chrome
- Timezone or geo mismatch with the proxy — fix with a different
  proxy or by locking the FP timezone
- GPU vendor doesn't match the template — usually means hardware
  acceleration is forced off; check `chrome_win64\` build flags

## 4. (Optional) Warm up the session

Sidebar → **🍪 Session/Cookies** → **Warmup robot** → pick a preset
(general / medical / tech / news / mobile) → **Run now**. This visits
7-8 real sites with cookie-consent auto-clicker so the profile
doesn't look like a brand-new bot on its first navigation.

Watch the run-history table — `succeeded` count should match
`planned` for a clean pass.

## 5. Your first script run

Sidebar → **Scripts** → pick the default flow or build your own with
the visual editor. The default does a single Google search and
captures any ads it finds.

Sidebar → **Profiles** → row → **▶ Start**. A real Chromium window
opens, runs the flow, snapshots cookies if the run is clean, and
exits. The **Runs** page logs the result; the **Logs** page has a
live tail.

## 6. (Optional) Schedule it

Sidebar → **Scheduler** → pick a mode:

- **Simple** — N runs/day spread over your active hours with jitter
- **Interval** — fixed gap between runs
- **Cron** — full 5-field cron expression (live "next 5 runs"
  preview)

Pick which profiles run, save. The `python -m ghost_shell scheduler`
process picks up the schedule the next iteration.

## What next

- Add **proxies** in bulk (Sidebar → Proxy → Import) — supports 9
  formats including `user:pass@host:port` and JSON
- Add **extensions** to the pool (Sidebar → 🧩 Extensions) and
  assign them to profiles on the profile detail page — useful for
  workflows that gate on specific browser extensions
- Use **Bulk create** when you need many profiles at once — see
  [Bulk Create](Bulk-Create.md)
- Build a more elaborate flow — see
  [Flow Steps Reference](Flow-Steps-Reference.md)
