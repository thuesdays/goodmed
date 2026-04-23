# Ghost Shell

> Antidetect Chromium + Flask dashboard for competitive intelligence on
> Google Search. Uses a custom-patched Chromium (149) to mask the
> browser at the C++ engine level — no JavaScript shims, no Playwright,
> no undetected-chromedriver tricks.

**Status:** 29/30 self-check tests passing · Chromium 149 engine ·
UA spoofed as Chrome 147 stable · production-ready for monitoring

![Ghost Shell banner](./ghost-shell-banner.png)

---

## What it does

1. You list some **search queries** and your **own domains**.
2. Ghost Shell spins up the patched Chromium behind a proxy, goes to
   Google, runs each query, and records every sponsored ad on the SERP.
3. For each ad it finds, it runs a **Script** — an ordered pipeline
   of actions you build visually in the dashboard. Click the ad, dwell
   20 s, scroll, close tab — as a real user would.
4. Everything goes to a SQLite database you can review in the
   dashboard: who's advertising on your keywords, how often, which
   copy they use, how many times you interacted with them.

This is a tool for **paid-search competitive monitoring**. It's what
you'd do manually in 10 minutes a day, automated 24/7 with a
fingerprint clean enough to not trigger Google's bot defenses.

---

## Why custom Chromium and not just Selenium

Selenium out of the box leaves a dozen detection markers:
`navigator.webdriver`, specific UA-CH header gaps, `$cdc_...` properties
on `window`, Canvas/WebGL noise patterns, timezone mismatches, empty
`navigator.mediaDevices`, broken Battery API, etc. Most "antidetect"
browsers patch these with **JavaScript shims** that themselves leave
detectable patterns — the shim itself is a fingerprint.

Ghost Shell patches the **C++ engine directly**. Every masked API
returns its faked value from the payload before JavaScript ever sees
it — identical to a real Chrome install from the outside. The patch
set covers:

- `navigator.webdriver`, plugins, UA, platform
- Canvas/WebGL/Audio fingerprint noise at the draw-call level
- Timezone + locale at Intl level
- UA Client Hints (JS API + `Sec-CH-UA-*` HTTP headers)
- Battery API, Permissions API, MediaDevices, SpeechSynthesis
- `performance.now()` sub-millisecond jitter
- MouseEvent `timeStamp` sub-millisecond jitter
- WebRTC IP leak hardening (command-line flag)

See [`CHROMIUM_PATCHES.md`](./CHROMIUM_PATCHES.md),
[`CHROMIUM_PATCHES_2.md`](./CHROMIUM_PATCHES_2.md) and
[`CHROMIUM_PATCHES_3.md`](./CHROMIUM_PATCHES_3.md) for the exact
source-tree diffs.

---

## Stack

| Layer          | Tech                                                 |
|----------------|------------------------------------------------------|
| Engine         | **Chromium 149** built from source with our patches  |
| UA spoof       | **Chrome 147 stable** (most common real version)     |
| Orchestration  | Python 3.11+ with Selenium                           |
| Dashboard      | Flask + vanilla JS SPA (no build step)               |
| State          | SQLite (`ghost_shell.db`)                            |
| Target OS      | Windows · macOS · Linux (build host-specific)        |

---

## Getting started

### 1. Clone

```powershell
cd F:\projects\
git clone https://github.com/YOUR-HANDLE/ghost_shell_browser.git
cd ghost_shell_browser
```

On macOS/Linux the layout is identical; use `~/projects/ghost_shell_browser`
or wherever you prefer.

### 2. Install Python deps

```powershell
pip install -r requirements.txt
```

### 3. Build the patched Chromium (one-time, ~2-4 hours)

Full walkthrough: [`CHROMIUM_PATCHES.md`](./CHROMIUM_PATCHES.md).

Short version:

```powershell
# Pull the Chromium source to F:\projects\chromium\ (~100 GB disk!)
# Apply the three patch sets from CHROMIUM_PATCHES*.md
# Build:
cd F:\projects\chromium\src
autoninja -C out\GhostShell chrome chromedriver

# Deploy the binary into this repo:
cd F:\projects\ghost_shell_browser
.\deploy-ghost-shell-flat.bat      # Windows
./deploy-ghost-shell-flat.sh       # macOS/Linux
```

**No time to build?** Run the dashboard in "headless control" mode: the
UI works with stock Chrome for browsing stats, but the monitor run
needs the patched binary to avoid detection.

### 4. Start the dashboard

```powershell
python dashboard_server.py
```

Browser opens to <http://127.0.0.1:5000> automatically.

### 5. Configure

In the dashboard:

1. **Domains page** → set your own domains, target domains, and
   optional block list. (Search queries are now configured inside
   `loop` steps on the Scripts page, not here.)
2. **Proxy page** → enter a proxy URL (residential rotating works
   best — static datacenter IPs catch captchas fast).
3. **Profiles page** → Create a profile. Pick a device template
   (office laptop, gaming desktop, etc.) — Ghost Shell auto-generates
   a consistent fingerprint payload.
4. **Scripts page** → build your run.

### 6. Build a script

The Scripts page has three sections:

- **Main script** — top-level action list that drives the whole run.
  The key action here is `loop`: you supply a list of items (query
  strings, URLs, whatever) and a set of nested steps to run for each
  item. Inside nested steps, reference the current item with
  `{item}` (or a custom variable name). Other top-level actions:
  `search_query` · `rotate_ip` · `pause` · `visit_url`.
- **Per-ad: competitor ads** — what to do when an ad is detected
  that's not your own. `click_ad`, `read`, `scroll`, `back`,
  `close_tab` — realistic user exploration.
- **Per-ad: target-domain ads** — same but for ads whose domain is
  in your Target Domains list. Typically lighter (record-only) so
  you don't waste CPC money interacting with yourself.

**If Main script is empty**, Ghost Shell runs a legacy default:
iterate every query in Domains, run the competitor pipeline for each
ad found, sleep between queries. Leave it empty if you don't need the
extra control.

#### Example Main script

```
loop:
  items:    ["best laptops", "gaming chair", "smart home hub"]
  item_var: query
  shuffle:  true
  steps:
    - pause          (min_sec: 2, max_sec: 5)
    - search_query   (query: "{query}")
    - rotate_ip      (wait_after_sec: 3)
```

Each iteration picks one query, sleeps a random 2–5 s, runs the search
(which dispatches the per-ad pipeline for every ad found), then forces
a proxy rotation before moving on.

### 7. Run it

Click the green **▶ Start** button in the sidebar. Watch live logs on
the Logs page or stats on Overview.

---

## Dashboard tour

| Page        | What's inside                                                |
|-------------|--------------------------------------------------------------|
| Overview    | 24h hero stats, 7-day chart, top competitors, profile health |
| Profiles    | Create / regenerate / preview fingerprints                   |
| Domains     | My domains · target domains · block list                     |
| Proxy       | Live IP diagnostics · rotation test · per-IP usage stats     |
| Competitors | Every ad ever captured, aggregated by domain                 |
| Behavior    | Timing ranges · naturalness toggles · captcha key            |
| Scripts     | Visual builder for the three pipelines                       |
| Runs        | History of every monitor run with outcome                    |
| Scheduler   | Cron-like runs                                               |
| Logs        | Live tail + historical                                       |
| Settings    | Export / import config as a portable JSON bundle             |

The sidebar shows a small pill under "Ghost Shell" with the current
engine and UA versions — e.g. `Chromium 149 · UA Chrome 147`.

---

## Project layout

```
F:\projects\ghost_shell_browser\
├─ main.py                      # Entry point of a run
├─ dashboard_server.py          # Flask app
├─ ghost_shell_browser.py       # Chromium + Selenium wrapper
├─ action_runner.py             # Script + per-ad pipeline executor
├─ device_templates.py          # Fingerprint generator
├─ db.py                        # SQLite schema + ops
├─ rotating_proxy.py            # Proxy pool + IP tracking
├─ proxy_diagnostics.py         # Live IP / geo / WebRTC leak probes
├─ session_quality.py           # Per-profile captcha/block tracking
├─ platform_paths.py            # Windows/macOS/Linux path resolution
├─ scheduler.py                 # Cron-like runs
├─ watchdog.py                  # Kills stuck runs
│
├─ ghost_shell_config.h/.cc     # C++ code that lives in Chromium too
├─ ghost_shell_ua_override.h/.cc # Browser-process UA hook
│
├─ dashboard/                   # Static SPA
│   ├─ index.html
│   ├─ css/main.css
│   ├─ js/
│   │   ├─ app.js               # Router
│   │   └─ pages/               # One file per tab
│   └─ pages/                   # HTML fragments
│
├─ chrome_win64/                # Deployed custom Chromium (git-ignored)
│   ├─ chrome.exe
│   └─ chromedriver.exe
│
├─ profiles/                    # Per-profile data (git-ignored)
│   └─ <name>/
│       ├─ user_data/           # Chrome profile dir
│       ├─ payload_debug.json
│       ├─ selfcheck.json
│       └─ logs/
│
└─ ghost_shell.db               # SQLite state (git-ignored)
```

---

## Export / Import configuration

Settings page → Download bundle. Produces a single JSON file with:

- Dashboard config (queries, domains, behavior timings, proxy URL)
- Profile definitions
- All three pipelines (main_script, post_ad, on_target)

History (runs, events, logs, IP history, captured competitors) is
**not** included — bundles are portable "setup snapshots".

Importing supports **merge** (default, safe) and **replace** (wipes
current config first).

---

## Requirements

- **Python 3.11+**
- **~500 MB** on disk for this repo
- **~100 GB** on disk if you build Chromium locally
- **Residential or mobile proxy** strongly recommended — datacenter
  IPs hit captcha walls within minutes on Google

---

## License

Personal / research use. This repo doesn't distribute Chromium binaries
— you build them from source using Google's BSD-licensed Chromium tree
and our patches.

---

## Documentation

- [`CHROMIUM_PATCHES.md`](./CHROMIUM_PATCHES.md) — initial patches (webdriver, plugins, canvas, audio, UA, screen, timezone, GPU)
- [`CHROMIUM_PATCHES_2.md`](./CHROMIUM_PATCHES_2.md) — Battery + Permissions + device_memory W3C clamp
- [`CHROMIUM_PATCHES_3.md`](./CHROMIUM_PATCHES_3.md) — UA Client Hints (JS API + HTTP headers)
- [`DATABASE.md`](./DATABASE.md) — SQLite schema reference
- [`DASHBOARD.md`](./DASHBOARD.md) — dashboard internals
- [`MACOS_BUILD.md`](./MACOS_BUILD.md) — Chromium build on macOS
- [`MACOS_SETUP.md`](./MACOS_SETUP.md) — dashboard-only mode and SSH-tunnel usage

---

**Engine:** Chromium 149 · **UA pool:** Chrome 143–147 (weighted toward current stable) · **Python:** 3.11+ · **Cross-platform:** Windows · macOS · Linux
