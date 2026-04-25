# Ghost Shell Anty

> Self-hosted antidetect browser + control dashboard for Google Ads
> competitive intelligence. Patched Chromium 149 at the C++ level —
> no JavaScript shims, no `undetected-chromedriver` tricks. Runs each
> profile through its own coherent fingerprint, proxy, cookie pool,
> and behavior pipeline.

| | |
|---|---|
| **Engine** | Chromium 149.0.7805 (custom build, stealth patches in C++) |
| **Stack** | Python 3.11+ · Flask dashboard · SQLite · Selenium 4 + CDP |
| **Platform** | Windows 10/11 (x64) — Linux/macOS source-buildable |
| **Status** | Production-ready · 11 device templates · 8 vault kinds · cron scheduler |

---

## Install (end users — Windows)

1. Download **`GhostShellAntySetup.exe`** from the
   [Releases page](https://github.com/thuesdays/ghost_shell_browser/releases).
2. Double-click. The wizard:
   - picks `%LOCALAPPDATA%\GhostShellAnty` as install folder (changeable),
   - silently installs Python 3.13 if you don't already have 3.11+,
   - copies the patched Chromium binary (`chrome_win64\` ≈ 600 MB),
   - creates a venv and pip-installs every Python dependency,
   - drops Desktop + Start menu shortcuts,
   - on the last page: optional **Launch dashboard now** checkbox.
3. After install the dashboard opens at `http://127.0.0.1:5000`.

Uninstall via Settings → Apps & features. The uninstaller stops any
running browser, removes the venv + DB + logs.

---

## Develop from source

```powershell
git clone https://github.com/thuesdays/ghost_shell_browser.git
cd ghost_shell_browser

python -m venv .venv
.\.venv\Scripts\activate

# Pulls every dep including cryptography (vault) and selenium
pip install -r requirements.txt

# Chromium binary lives outside git (too large for GitHub).
# Either copy your local build into chrome_win64\, or:
.\scripts\download_chromium.ps1                    # pulls latest release asset

python -m ghost_shell dashboard                    # → http://127.0.0.1:5000
```

Three CLI entrypoints:

| Command | What it does |
|---|---|
| `python -m ghost_shell monitor` | runs one ad-monitoring pass for the active profile |
| `python -m ghost_shell dashboard` | starts the Flask dashboard on `:5000` (auto-opens browser) |
| `python -m ghost_shell scheduler` | background loop that fires `monitor` runs on a schedule |

VS Code launch configs for all three are in `.vscode/launch.json`.

---

## Architecture

```
ghost_shell/
├── core/         platform paths, log banners, process reaper
├── db/           SQLite layer (3500+ LOC, migrations idempotent)
├── fingerprint/  device templates · generator · validator · selftest
├── proxy/        diagnostics · forwarder · pool · rotation provider
├── profile/      manager · enricher · validator · pool
├── session/      cookies · cookie pool (resurrection) · warmup robot
├── browser/      runtime (CDP-driven) · watchdog · traffic collector
├── actions/      script flow runner (15+ actions, 15+ conditions)
├── scheduler/    pure-Python cron parser + 3 schedule modes
├── dashboard/    Flask server (4500+ LOC, 70+ endpoints)
├── accounts/     Fernet-encrypted vault (8 kinds: account/wallet/api_key/…)
├── __main__.py   runpy dispatcher for monitor|dashboard|scheduler
└── main.py       monitor entrypoint
```

Standalone scripts live in `scripts/`, tests in `tests/`, and the

---

## Features (dashboard pages)

- **Overview** — 24h KPIs, 7-day chart, fingerprint health rollup,
  per-profile self-check status, traffic-card.
- **Profiles** — table of profiles with per-row Start/Stop, status
  badges, "Set as default", quick-jump links to fingerprint/session.
- **Profile detail** — script + proxy + fingerprint assignments,
  cookies/session summary, **Danger zone** at the bottom (reset
  blocks / clear history / delete profile).
- **Fingerprint** — coherence editor: 11 device templates (7 desktop +
  4 mobile), category filter, score badge with grade colours,
  field-by-field editor with locks, history with restore, live
  self-test that launches a real browser and compares configured vs
  actual, **dual-mode toggle** (desktop ↔ mobile, switches active FP
  instantly).
- **🍪 Session/Cookies** — left-side profile nav (vertical card list
  with active-border accent), 4 tabs:
  - **Warmup robot** — 5 categorized presets (general, medical, tech,
    news, mobile), preset cards, run-now + history table. Real-browser
    visits with cookie-consent auto-clicker.
  - **Cookies** — live table per next-run, filter, import/export,
    flag indicators (S/H/N/L).
  - **Snapshots** — cookie pool with auto-snapshot after clean runs;
    restore queues injection at next launch.
  - **Chrome import** — pulls real history/bookmarks from your own
    Chrome install (WAL-safe even while Chrome is open).
- **Proxy** — library of named proxies with cached diagnostics
  (geo/ISP/latency/risk), bulk import (9 formats), per-profile
  assignment.
- **Domains** — your own domains (skipped as competitors), targets
  (trigger on-target action chain), block list.
- **Competitors** — ad-intel dashboard: trend chart (line/stacked),
  activity badges (NEW/ACTIVE/QUIETING), per-row 7-day sparklines,
  expandable rows with titles + display URLs + matched queries,
  share-of-voice tab, inline 🎯/🏠/🚫 actions to push a domain into
  the Domains lists, CSV/JSON export.
- **Behavior** — typing/dwell/scroll/refresh timing knobs, captcha
  recovery policy, traffic block-list patterns.
- **Scripts** — visual flow builder for the per-ad pipeline. 15+
  actions (click, dwell, scroll, type, swipe, touch_click, …) and
  15+ conditions (ads_found, captcha_present, var_*, …). Library
  with named scripts, one is default, profiles can override.
- **🔐 Accounts & Vault** — encrypted local password manager. Master
  password unlocks an in-memory key (PBKDF2-HMAC-SHA256, 200k iters →
  Fernet). 8 kinds:
  - `account` (login + password + 2FA),
  - `email` (with IMAP/SMTP fields),
  - `social`, `crypto_wallet` (seed + private key),
  - `api_key`, `totp_only`, `note`, `custom` (free-form fields).
  Each kind has its own form layout. TOTP code revealed inline,
  one-click copy. RFC 6238 compliant.
- **Runs** — table of every monitor run with exit code, captcha count,
  duration, profile, log link.
- **Traffic** — per-profile + per-host bandwidth tracker; Network.*
  CDP events drive the counters.
- **Scheduler** — three modes:
  - **Simple** (density) — N runs/day spread over active hours w/ jitter,
  - **Interval** — fixed gap between runs,
  - **Cron** — full 5-field cron expression with live "next 5 runs"
    preview. Active days-of-week (Mon-Sun) chips on top of all modes.
  Profile selection card-grid with search + bulk (All/None/Invert/✓Healthy).
  Group launch (parallel/serial) when monitoring needs to fire several
  profiles per iteration.
- **Logs** — terminal-style live tail with level filter + grep,
  per-run timestamp grouping, ring buffer (10k lines).
- **Settings** — VSCode-style sidebar: paths · UA spoof range · proxy
  settings · self-check cadence · auto-enrich · session retention ·
  traffic retention · danger-zone wipe.

---

## Anti-detection layers

Stacked top-to-bottom — each layer hides a different signal:

1. **C++ Chromium patches** (in the binary itself):
   - `navigator.webdriver` → `false`
   - canvas/WebGL/audio noise (sub-pixel ratio, ±1 LSB)
   - sub-millisecond timing jitter (`performance.now`, `Date.now`)
   - User-Agent + UA-CH + sec-ch-ua-platform spoof
   - Battery / Permissions / Bluetooth presence
2. **Fingerprint coherence** — UA, GPU vendor, fonts, timezone all
   come from one device template; can't drift.
3. **CDP runtime emulation** — for mobile profiles: viewport, touch,
   DeviceMotion, DeviceOrientation, mobile UA + UA-CH platform.
4. **Behavior** — randomized typing speed, mouse paths with
   curve-fitting, dwell times sampled from human distributions,
   gentle scroll with pauses, on mobile profiles → CDP touch swipes.
5. **Session warmup** — fresh profiles visit 7-8 realistic sites
   first (cookie-consent banners auto-clicked) so Google sees
   organic history before the first SERP query.
6. **Cookie pool** — clean-run snapshots; if a session degrades,
   restore the last good one rather than starting cold.
7. **Proxy intelligence** — geo/ASN/IP-type validation per run,
   exit-IP gating against expected country/timezone.

---

## Installation

End users: grab the latest **`GhostShellAntySetup.exe`** from
[Releases](https://github.com/thuesdays/ghost_shell_browser/releases)
and run it. The installer ships the patched Chromium runtime, a
Python venv, and every dependency — no git or pip needed.

The installer build pipeline is maintainer-only and lives in a
private toolchain; this repo only carries the source and the
download script for the patched Chromium binary.

---

## Tech notes

- **DB migrations** are idempotent: every `_ensure_column` checks
  before adding. Safe to upgrade in-place across versions.
- **Chromium binary is NOT in git** — too large for GitHub's 100MB-
  per-file limit. `chrome_win64/` is `.gitignore`-d; for a
  source-only checkout, run `.\scripts\download_chromium.ps1` to
  pull the matching release asset. Releases also include a
  ready-to-run `GhostShellAntySetup.exe` for end users.
- **Refactor v0.2.0**: project moved from 39 flat files at the root
  into a proper `ghost_shell/` package. Old paths preserved in
  `_legacy/` for reference only — not on `sys.path`, not shipped.
- **Vault crypto**: master password → PBKDF2-HMAC-SHA256 (200k iters)
  → 32-byte key → Fernet (AES-128-CBC + HMAC-SHA256). Key is in
  process memory only; lost on restart, user re-unlocks. Salt and a
  short verification token live in `config_kv` so we can confirm the
  master without decrypting anything real.
- **Cron parser** is pure-stdlib (no `croniter` dependency). Supports
  `*`, `*/N`, `N-M`, `N,M,P`, `N-M/S` across the standard 5 fields.

---

## License

MIT — see [`LICENSE`](LICENSE).

This software is provided for legitimate uses (competitive intelligence
on your own ad spend, QA, accessibility automation, etc). Users are
responsible for compliance with the terms of service of any sites they
automate against.

---

## Contributing

Contributions are very welcome — bug reports, doc fixes, new device
templates, UX polish, translations, feature ideas. You don't need to be
a fingerprint expert to help.

**Easy first contributions:**

- A new device template (RTX 5080 desktop, M4 MacBook, current flagship
  Android) in [`device_templates.py`](ghost_shell/fingerprint/device_templates.py)
  with a coherence test
- A new warmup preset for a vertical you understand (e-commerce,
  fashion, gambling, …)
- CSS polish on the dashboard
- Issues with reproduction steps when something breaks

**Before opening a PR:**

1. `pytest tests/` must pass
2. New `.py` files include the author header (`__author__` / `__email__`)
3. New comments and docstrings in English
4. New device templates need the full field set + a unit test asserting
   the generated fingerprint scores ≥70 in the validator

For larger architectural changes, open an issue first.

---

## Support the project

Ghost Shell Anty is MIT-licensed and free forever — no per-profile fees,
no cloud subscription, no unlock tiers. It's maintained on personal time.
If the tool saves you money you'd otherwise spend on a commercial
antidetect browser, consider chipping in to keep development active.

Donations directly fund Chromium rebases (every major release means
re-applying the C++ stealth patches), new device templates, selfcheck
rule updates as anti-detection sites move, and CI infrastructure.

### Crypto wallets

| Network | Address | |
|---|---|---|
| **Ethereum / EVM** (ERC-20 USDC, USDT, ETH) | `0xbd9b0b717139542632b5c45df7096dB2484976D5` | [Etherscan](https://etherscan.io/address/0xbd9b0b717139542632b5c45df7096dB2484976D5) |
| **Solana** (SOL, SPL USDC, USDT) | `FbK1eZHPhQM8NYKntRZsUqQ3FWxgj9mWwEXr2o54Qdck` | [Solscan](https://solscan.io/account/FbK1eZHPhQM8NYKntRZsUqQ3FWxgj9mWwEXr2o54Qdck) |

For copy/paste convenience:

```text
ETH / ERC-20 :  0xbd9b0b717139542632b5c45df7096dB2484976D5
Solana       :  FbK1eZHPhQM8NYKntRZsUqQ3FWxgj9mWwEXr2o54Qdck
```

Any amount is appreciated. If you donate from a vanity wallet you'd
like credited in the README, drop an email below — happy to add a
Sponsors row in the next release.

### Other ways to help

- ⭐ **Star the repo** — costs nothing, helps with discovery
- 📣 **Tell a friend** running ad ops at scale — word of mouth is most
  of how this tool finds users
- 🐛 **Report bugs with logs** — the dashboard's Logs page has a
  Download button that captures everything maintainers need
- 📝 **Write a blog post / video** if you build something interesting
  on top of Ghost Shell — happy to link back

---

## Links

- **Issues**: [GitHub Issues](https://github.com/thuesdays/ghost_shell_browser/issues)
- **Wiki**: [Ghost Shell Anty Wiki](https://github.com/thuesdays/ghost_shell_browser/wiki)
- **Releases**: [Latest installer](https://github.com/thuesdays/ghost_shell_browser/releases)
- **Maintainer**: [@thuesdays](https://github.com/thuesdays) · thuesdays@gmail.com
