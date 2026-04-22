# Ghost Shell

Self-hosted Google Ads monitor with a custom antidetect Chromium browser.
Tracks competitor ads for your brand queries, runs on schedule, shows
everything in a live dashboard.

**Stack**

- **Custom Chromium 149** (Windows build) with C++ patches in Blink for
  fingerprint spoofing — navigator, screen, canvas, WebGL, audio,
  timezone, languages. No JS injection — everything at the C++ layer
  where detection scripts can't reach.
- **Python 3.11+** for automation, proxy handling, SQLite persistence.
- **Flask + vanilla JS** dashboard (auto-opens in your browser on start).
- **SQLite** as single source of truth.

---

## Table of contents

- [Architecture](#architecture)
- [Requirements](#requirements)
- [Quick start on Windows](#quick-start-on-windows)
- [Running on macOS](#running-on-macos)
- [Project layout](#project-layout)
- [Dashboard overview](#dashboard-overview)
- [Configuration](#configuration)
- [How a single run works](#how-a-single-run-works)
- [Troubleshooting](#troubleshooting)

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                       Ghost Shell                            │
│                                                              │
│  ┌──────────────┐   ┌──────────────┐   ┌────────────────┐    │
│  │  Dashboard   │   │    main.py   │   │   scheduler    │    │
│  │  (Flask UI)  │──▶│  (monitor    │◀──│   (cron-like)  │    │
│  │              │   │   run)       │   │                │    │
│  └──────┬───────┘   └───────┬──────┘   └────────────────┘    │
│         │                   │                                │
│         │        ┌──────────▼──────────┐                     │
│         └───────▶│  SQLite (ghost_shell.db)                  │
│                  │  profiles · runs · fingerprints ·         │
│                  │  competitors · events · logs · config     │
│                  └───────────────────────────────────────────┘
│                               │                              │
│  ┌────────────────────────────▼───────────────────────┐      │
│  │  Ghost Shell browser layer                         │      │
│  │                                                    │      │
│  │  Selenium ──▶ chromedriver ──▶ Custom Chromium     │      │
│  │                                 + C++ stealth      │      │
│  │                                 patches            │      │
│  └────────────────────────────────────────────────────┘      │
└──────────────────────────────────────────────────────────────┘
                              │
                      ┌───────▼────────┐
                      │ Rotating proxy │
                      │  (asocks pool) │
                      │    Ukraine     │
                      └───────┬────────┘
                              │
                         google.com
```

**A single run**: device template builder produces a deterministic
fingerprint (SHA256 of `profile_name` → seed). chrome launches with
`--ghost-shell-payload=<base64-json>`. Our C++
`GhostShellConfig::Initialize()` parses it and exposes the values to
Blink via hooks inside Navigator / Screen / Canvas / WebGL / Audio /
Timezone. Selenium drives searches. Results go to SQLite.

---

## Requirements

### Windows — full setup (builds Chromium + runs everything)

| Tool              | Version / source                             |
|-------------------|----------------------------------------------|
| Windows 10/11 x64 | tested on 11                                 |
| Visual Studio 2022| + "Desktop development with C++" + Win11 SDK |
| depot_tools       | Google's build tooling for Chromium          |
| Python 3.11+      | with `venv`                                  |
| Disk              | ~150 GB for Chromium source + build output   |
| RAM               | 16 GB minimum, 32 GB recommended             |

### macOS — full setup (builds Chromium too)

| Tool              | Version / source                             |
|-------------------|----------------------------------------------|
| macOS 13+         | Intel and Apple Silicon both fine            |
| Xcode CLT         | `xcode-select --install`                     |
| Python 3.11+      | `brew install python@3.11`                   |
| Disk              | ~150 GB for Chromium source + build          |
| RAM               | 16 GB minimum                                |

Builds the same Chromium+patches as on Windows, deploys to `chrome_mac/`.
After that, Ghost Shell runs end-to-end on Mac. Step-by-step instructions
in **[MACOS_BUILD.md](MACOS_BUILD.md)**.

### macOS / Linux — dashboard only (no monitoring)

If you don't want to build Chromium on Mac, you can still use the
dashboard for config, proxy diagnostics and viewing data — see
**[MACOS_SETUP.md](MACOS_SETUP.md)** for the "dashboard-only" and
"SSH-tunnel to Windows" options.

---

## Quick start on Windows

### 1. Python project

```powershell
cd F:\projects\goodmedika
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Custom Chromium build (one-time, 2–4 hours first build)

```powershell
# Get depot_tools
git clone https://chromium.googlesource.com/chromium/tools/depot_tools.git F:\projects\depot
# Add F:\projects\depot to PATH

# Fetch Chromium source
mkdir F:\projects\chromium
cd F:\projects\chromium
fetch --nohooks chromium
cd src
gclient runhooks

# Apply Ghost Shell patches
#   - ghost_shell_config.h  → third_party/blink/renderer/platform/
#   - ghost_shell_config.cc → third_party/blink/renderer/platform/
#   - ~12 edits in existing Chromium files (Navigator, Screen, Audio,
#     Canvas, WebGL, Timezone, etc.)
# Full list and exact diffs: CHROMIUM_PATCHES.md

# Create args.gn at out\GhostShell\args.gn:
#   is_debug           = false
#   symbol_level       = 0
#   is_component_build = false
#   enable_direct_composition = false

gn gen out\GhostShell
autoninja -C out\GhostShell chrome chromedriver crashpad_handler
```

Incremental rebuilds after editing one `.cc` file take ~1-2 minutes.

### 3. Deploy to the project folder

```powershell
cd F:\projects\goodmedika
.\deploy-ghost-shell-flat.bat
```

Copies `chrome.exe`, `chromedriver.exe`, `chrome.dll`, SxS manifest,
`.pak` files, locales etc. into `chrome_win64\`.

### 4. Verify

```powershell
python platform_paths.py
# → Platform:     windows
# → Chrome found: F:\projects\goodmedika\chrome_win64\chrome.exe
# → Driver found: F:\projects\goodmedika\chrome_win64\chromedriver.exe

python test_chromedriver.py
# → Opens Chrome, navigates to google.com, closes. No errors.
```

### 5. Configure

```powershell
python dashboard_server.py
# → Dashboard auto-opens at http://127.0.0.1:5000
```

In the dashboard:

- **Proxy** page — paste your rotating proxy URL, enable "Auto-rotate on
  start", set expected country = Ukraine. Run **🧪 Rotation test** — aim
  for ≥90% Ukraine exits.
- **Profiles** page — **✨ Create profile** with a random name and
  template = "auto". The default `profile_01` is already there.
- **Search** / **Behavior** — your query list. Our defaults: `гудмедика`,
  `гудмедіка`, `goodmedika`.

### 6. First real run

Hit **▶ Start** in the dashboard sidebar. Watch the log panel:

```
▶ RUN #1 STARTED
  Profile : profile_01
  Template: office_laptop_intel (Chrome 132.0.6834.210)
  Locale  : uk-UA (Europe/Kyiv, UTC+03:00)
  Exit IP : 193.32.154.239 [Ukraine / DataWeb]
  …
 [1/3] "гудмедика"  → 2 ads, 2 competitors in 10.1s
 [2/3] "гудмедіка"  → 3 ads, 2 competitors in 8.7s
 [3/3] "goodmedika" → 1 ads, 1 competitors in 9.3s
✓ RUN #1 COMPLETED
  Duration: 1m 36s
```

---

## Running on macOS

You have **two paths** on Mac depending on how much work you want to
put in.

### Option A — Full build (recommended long-term)

Build the same patched Chromium on Mac. Dashboard + ▶ Start + monitoring
all work identically to Windows. See **[MACOS_BUILD.md](MACOS_BUILD.md)**
for the exact 7-step guide. First build takes 1-4 hours; after that
everything Just Works.

```bash
# After following MACOS_BUILD.md:
python test_chromedriver.py       # smoke test
python dashboard_server.py        # ▶ Start from dashboard — real runs
```

### Option B — Dashboard only / remote monitoring

Don't want to spend 4 hours building? You can still use the dashboard
as a frontend and have the actual runs happen on the Windows box:

```bash
# On Mac
cd ~/ghost-shell
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python dashboard_server.py       # dashboard opens in Safari/Chrome
```

**Works fully:**
- Every dashboard page (Overview, Profiles, Proxy, Runs, Scheduler, …)
- **✨ Create profile** with **🔮 Preview fingerprint**
- **🎲 Regenerate fingerprint**
- **🧪 Rotation test** — uses plain `requests`, no Chrome needed
- **🛰 Proxy diagnostics**, **⚡ Rotate IP**
- All historical data

**Doesn't work:** **▶ Start** / `python main.py` — needs the Chromium
binary. Use SSH tunnel (`ssh -L 5000:localhost:5000 windows-box`) or
have a shared `ghost_shell.db` between machines. Details in
[MACOS_SETUP.md](MACOS_SETUP.md).

---

## Project layout

```
goodmedika/
├── ghost_shell.db                  # SQLite — all state lives here
├── chrome_win64/                   # Deployed Chromium build (Windows)
│   ├── chrome.exe
│   ├── chrome.dll
│   ├── chromedriver.exe
│   └── …resource files, locales, etc.
├── profiles/
│   └── profile_01/                 # Per-profile user-data-dir
│       ├── Default/                # Chrome's standard profile dir
│       ├── payload_debug.json      # Current fingerprint (copy of DB)
│       ├── logs/YYYYMMDD.log       # Per-day per-profile log
│       └── chromedriver.log        # CDP driver log (debugging)
├── dashboard/                      # Flask SPA
│   ├── index.html
│   ├── css/main.css
│   ├── js/
│   │   ├── api.js, utils.js, app.js, config-form.js, runner.js
│   │   └── pages/
│   │       ├── overview.js, profiles.js, profile-detail.js,
│   │       ├── runs.js, proxy.js, scheduler.js, logs.js,
│   │       └── behavior.js, actions.js, search.js, competitors.js
│   └── pages/
│       ├── overview.html, profiles.html, profile.html,
│       ├── runs.html, proxy.html, scheduler.html, logs.html,
│       └── behavior.html, actions.html, search.html, competitors.html
│
├── main.py                         # Monitor run entrypoint
├── dashboard_server.py             # Flask app, auto-opens browser
├── scheduler.py                    # cron-like runner
├── db.py                           # SQLite layer + defaults
├── ghost_shell_browser.py          # Wraps selenium + custom Chromium
├── device_templates.py             # Deterministic fingerprint builder
├── platform_paths.py               # OS-aware path resolution
├── proxy_diagnostics.py            # IP / geo / timezone checks
├── proxy_forwarder.py              # Local proxy frontend for chrome
├── rotating_proxy.py               # Provider-agnostic rotation tracker
├── session_quality.py              # Per-profile health metrics
├── session_manager.py              # Cookie import / session helpers
├── profile_enricher.py             # History/bookmarks seeding
├── log_banners.py                  # Pretty log banners
├── watchdog.py                     # Chrome liveness monitor
│
├── build-ghost-shell.bat           # Windows: full build + deploy
├── deploy-ghost-shell-flat.bat     # Windows: deploy only
├── deploy-ghost-shell-flat.sh      # macOS / Linux deploy (for future)
├── chrome-launcher.bat             # Standalone chrome launcher
├── test_chromedriver.py            # Minimal selenium smoke test
├── bisect_flags.py                 # Which chrome flag crashes it?
├── diagnose-chrome.bat             # Windows chrome diagnostic battery
├── test_proxy_rotation.py          # 10 rotations, country breakdown
│
├── ghost_shell_config.h            # C++ patch — header
├── ghost_shell_config.cc           # C++ patch — implementation
├── CHROMIUM_PATCHES.md             # What Chromium files to edit
│
├── README.md                       # This file
├── DASHBOARD.md                    # Dashboard internals / dev guide
├── requirements.txt                # Python deps
└── .env                            # local secrets (gitignored)
```

---

## Dashboard overview

Gradient-accented sidebar, auto-navigation on start, full-height layout.

### Overview

- **Hero panel** — 24h counts for searches / ads / captchas / success
  rate, each with a `▲ 12% vs yesterday` trend badge.
- **Stats grid** — lifetime totals.
- **Daily chart** — 7-day line chart of searches/captchas/empty results.
- **Recent activity** — last 8 runs with status dots and time-ago.
- **Top competitors** — top-10 competitor domains matched.
- **Profile health** — per-profile rollup: template, self-check
  passes/total, captcha rate (colour-coded), status.

### Profiles

List of all browser profiles.  Per-row context menu (⋮): start, stop,
set active, delete.  Column picker to show/hide columns.

- **✨ Create profile** — modal with:
  - Auto-suggested name (`profile_02`, etc.) + 🎲 random generator
  - Template picker (auto, or specific: `office_laptop_intel`, …)
  - Preferred language
  - **🔮 Preview** — shows the generated fingerprint (UA, screen, GPU,
    timezone, fonts count, plugin count, …) *before* you create it

Click a row → **Profile detail**:
- Main settings (chromium path, profile name, language)
- Self-check grid (13 probes for C++ stealth hooks)
- Current fingerprint JSON
- **🎲 Regenerate fingerprint** — re-rolls with a new seed; keeps the
  profile's browsing data (history, cookies)
- Clear history, Reset blocks, Delete profile

### Proxy

- **🛰 Live diagnostics** panel: current exit IP + country / city /
  timezone / ASN / type / detection risk. 🔄 Refresh, ⚡ Rotate IP.
- Geo-match and timezone-match checks (green/red).
- **🧪 Rotation test** — runs 5-30 requests through the proxy, shows a
  country breakdown bar chart + per-request IP table. Use this to
  verify your asocks country filter is actually working.
- **Geo lock** — expected country / timezone / mismatch mode:
  - `abort` — reject the run immediately
  - `rotate` — retry up to 5× to get the right country
  - `warn` — log and continue
- **Auto-rotate on run start** toggle.
- Proxy URL, pool, rotation API config.
- IP statistics table — uses, captchas, captcha rate, status per IP.

### Runs (history)

- Total / successful / failed counters
- **🗑 Clear history** toolbar — keep last 30 / 7 / 1 day or drop all
- Per-row: profile, duration, requests, ads, captchas, exit code, log link

### Scheduler

- Enable / disable toggle
- Mode: random delay within a window, or fixed interval
- Multi-profile selection (round-robin or random)
- Failure handling: skip on error, rotate proxy on captcha, stop on N
  consecutive failures
- Start / Stop the scheduler daemon from the dashboard

### Behavior / Actions / Logs / Competitors / Search

Config pages for post-click behaviour, action pipelines, live log tail,
competitor database browser, query list.

---

## Configuration

All config lives in **SQLite** (`config_kv` table), exposed through the
dashboard. No `config.yaml` to hand-edit.

Most useful keys:

| Key                                | Default                         | Notes                                       |
|------------------------------------|---------------------------------|---------------------------------------------|
| `browser.binary_path`              | `chrome_win64/chrome.exe` (Win) | Platform-aware — detects on start           |
| `browser.profile_name`             | `profile_01`                    | Active profile                              |
| `browser.preferred_language`       | `uk-UA`                         | Drives Accept-Language + navigator.language |
| `browser.expected_country`         | `Ukraine`                       | Used by geo-mismatch check                  |
| `browser.expected_timezone`        | `Europe/Kyiv`                   |                                             |
| `browser.geo_mismatch_mode`        | `rotate`                        | `abort` / `rotate` / `warn`                 |
| `browser.enrich_on_create`         | `True`                          | Seeds history + bookmarks on first run      |
| `proxy.url`                        | ""                              | `user:pass@host:port`                       |
| `proxy.is_rotating`                | `True`                          |                                             |
| `proxy.auto_rotate_on_start`       | `True`                          | Fresh IP before each run                    |
| `proxy.rotation_provider`          | `none`                          | `asocks` / `brightdata` / `generic` / `none`|
| `search.queries`                   | `["гудмедика", …]`              | Queries per monitor session                 |
| `search.my_domains`                | `["goodmedika.com.ua", …]`      | Own domains — excluded from competitors     |
| `watchdog.max_stall_sec`           | `180`                           | Kill chrome if no activity for this long    |

Edit from the dashboard or CLI:

```powershell
python -c "from db import get_db; get_db().config_set('browser.profile_name', 'profile_03')"
```

Env-var overrides for main.py / debugging:

| Env var                        | Effect                                           |
|--------------------------------|--------------------------------------------------|
| `GHOST_SHELL_PROFILE`          | Override active profile for one run              |
| `GHOST_SHELL_RUN_ID`           | Set by dashboard/scheduler — don't set manually  |
| `GHOST_SHELL_SKIP_PAYLOAD=1`   | Launch chrome without our C++ payload (debug)    |
| `GHOST_SHELL_SKIP_ENRICH=1`    | Skip profile enricher on first run (debug)       |
| `GHOST_SHELL_VERBOSE_CHROME=1` | Write chrome stderr to `<profile>/chrome_debug.log`|
| `GHOST_SHELL_NO_BROWSER=1`     | Don't auto-open dashboard in a browser           |

---

## How a single run works

High-level flow of `main.py` (same as dashboard "▶ Start"):

1. **Payload generation** — `DeviceTemplateBuilder(profile_name)`
   produces a deterministic ~8 KB JSON fingerprint: UA, screen,
   hardware, GPU, WebGL extensions, fonts, plugins, audio baseline,
   timezone, languages, media devices, battery, canvas noise. Same
   `profile_name` → same fingerprint forever (unless you regenerate).

2. **Profile enrichment** — if the profile dir is new, seed
   `History` / `Bookmarks` / `Top Sites` with 300-500 realistic visits
   from a Ukrainian-user browsing pattern. Makes the browser look
   aged.

3. **Proxy forwarder** — opens a local TCP socket on `127.0.0.1:N` that
   forwards HTTP/CONNECT traffic to the rotating proxy with auth baked
   in. Lets chrome's `--proxy-server` flag deal transparently with
   rotating credentials.

4. **Chromium launch** — plain `selenium.webdriver.Chrome` starts our
   chrome binary with `--ghost-shell-payload=<base64-json>`. Our C++
   `GhostShellConfig::Initialize()` decodes it and stashes values that
   Blink hooks read whenever JS touches `navigator.*`, `screen.*`,
   `Intl.DateTimeFormat`, `<canvas>.toDataURL`, WebGL getParameter,
   Audio APIs, etc.

5. **Self-check** — 13 probes via CDP to verify stealth is actually
   working: `navigator.webdriver`, `plugins.length`, no `cdc_…`
   globals, `chrome` object shape, UA ↔ payload match, hardware
   concurrency, language, screen width, pixel ratio, timezone, device
   memory, iframe consistency, no automation marks. Results → DB.

6. **Proxy diagnostics** — fetches exit IP from ipapi.co (ipwho.is
   fallback), checks WebRTC leak, checks geo-match against expected
   country. On mismatch + mode=`rotate` calls `force_rotate_ip()` up
   to 5×.

7. **Cookie warmer** — installs 11 pre-seeded Google consent cookies so
   we land on fresh SERPs instead of the EU consent banner.

8. **Search loop** — for each query:
   - Navigate to `google.com/search?q=…`
   - Wait for SERP containers
   - Detect captcha (`/sorry/`, `g-recaptcha`, "unusual traffic"
     text) → report IP as burned, skip
   - Parse ad blocks (`[data-text-ad]`, `.uEierd`, etc.), filter out
     our own domains
   - For each competitor ad → run action pipeline (click, scroll,
     read, return) with randomised timing

9. **Teardown** — flush DB, close driver, stop forwarder, release
   profile lock. Dashboard log updates with the green `RUN #N
   COMPLETED` banner.

10. **Manual close** — if the user closes the chrome window manually,
    the dashboard monitor thread (watching chrome via `psutil`)
    detects the disappearance within ~3 s, terminates main.py,
    updates status to "stopped".

---

## Troubleshooting

**Chrome silently crashes on launch, ~2-second duration**
Usually one of:
- Stale `SingletonLock` in profile dir → we auto-delete now.
- Corrupt `Preferences` JSON → we validate and discard if broken.
- `--accept-lang` flag → **removed**, was crashing our custom Chromium.
  Language is fully handled through the C++ payload instead.
- `--ghost-shell-payload` parsing bug → set
  `GHOST_SHELL_SKIP_PAYLOAD=1`; if that launches fine, C++ code is the
  culprit.
- Incompatible data from older Chrome → wipe `profiles/profile_01/`
  and let the enricher re-seed.

**"Cannot connect to chrome at 127.0.0.1:NNNNN"**
chromedriver version doesn't match Chrome. Fix: build chromedriver
from the same Chromium tree.
```powershell
autoninja -C out\GhostShell chromedriver
.\deploy-ghost-shell-flat.bat
```

**Multiple console windows flash during run**
`LOG(INFO)` in our C++ patch was going to stderr for every renderer
subprocess. Fixed by switching to `VLOG(1)` (silent unless `--v=1`)
plus `--disable-logging --log-level=3` and
`excludeSwitches=["enable-logging"]`.

**"Chrome is being controlled by automated test software" banner**
Suppressed with `excludeSwitches=["enable-automation"]` and
`useAutomationExtension=False`.

**Exit IP in wrong country (e.g. Argentina instead of Ukraine)**
asocks rotating pool returns random country unless:
1. Set "Behavior when proxy dies" = **Keep Connected** (not "Change for
   each request")
2. Pin Country/Region/City in asocks dashboard
3. Enable `geo_mismatch_mode = rotate` in our dashboard (up to 5
   retries)
4. Run **🧪 Rotation test** to verify ≥90 % Ukraine hit rate

**`device_memory_matches: False` in self-check**
W3C spec clamps `navigator.deviceMemory` to max 8 GB for privacy. Our
test now compares against `min(payload.device_memory, 8)`.

**Dashboard says "Running" but chrome is closed**
Fixed — dashboard runs a `psutil`-based child-process monitor and
terminates main.py within 3 s of chrome disappearing.

**METHOD NOT ALLOWED on profile create**
Flask was matching `/api/profiles/preview-fingerprint` against the
parameterised `/api/profiles/<n>` DELETE route. Fixed by moving
those endpoints to `/api/profile-templates` and
`/api/fingerprint/preview`.

**Rotating proxy gives 10 % non-Ukraine exits**
Normal for asocks. Our `geo_mismatch_mode = rotate` catches these
automatically before the search loop.

### Diagnostic scripts

```powershell
python platform_paths.py          # verify platform detection
python test_chromedriver.py       # smoke test — raw selenium + our chrome
python test_proxy_rotation.py     # 10 rotations, country breakdown
python bisect_flags.py            # which chrome flag is causing a crash
```

---

## License

Private project. C++ patches are derivative of Chromium (BSD).
