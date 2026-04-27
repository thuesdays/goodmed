# Profiles & Fingerprints

A profile is a complete browser identity: device fingerprint, proxy,
cookies, extensions, behaviour timing, and a script flow. Profiles
are isolated on disk (one `user_data_dir` each) and in DB rows.

## Device templates (11 total)

Each template is a coherent set of fields — UA, GPU vendor/renderer,
fonts, languages, timezone, platform, viewport — that match a real
device. Templates ship in
[`device_templates.py`](https://github.com/thuesdays/ghost_shell_browser/blob/main/ghost_shell/fingerprint/device_templates.py).

Current set:

**Desktop (7)**
- Win10 Chrome (mid-tier Intel + integrated GPU)
- Win11 Chrome (high-end gaming, RTX-class)
- Win11 Chrome (laptop, low-power)
- macOS Sonoma Chrome (M2 MacBook)
- macOS Sonoma Chrome (Intel iMac)
- Linux Chrome (Ubuntu, Mesa)
- ChromeOS

**Mobile (4)**
- Pixel 8 Pro (Android Chrome)
- Galaxy S24 (Android Chrome)
- iPhone 15 (iOS Safari masquerading as Chrome iOS)
- iPad Pro (iPadOS Safari)

Picking a desktop template gives the profile a desktop UA, fonts,
viewport, and skips the CDP touch-emulation. Picking mobile flips
all of those.

## Coherence

The validator enforces that fields can't drift away from the chosen
template. It catches:

- UA says Windows but timezone is `America/Los_Angeles` plus the
  proxy is in Berlin → mismatch
- UA says iPhone but `navigator.platform` is `Win32` → mismatch
- GPU renderer says NVIDIA but UA says Mac M2 → mismatch
- Audio context sample rate doesn't match the OS-typical default
  → mismatch

Each profile gets a coherence score `0..100`. Scores ≥ 80 are
generally usable; below 70 expect detection. The Fingerprint editor
shows the score with a grade colour and the underlying report
explaining each deduction.

## History and Restore

Every successful FP edit creates a row in the `fingerprints` table.
Only one row per profile has `is_current=1`. The Fingerprint editor
has a **History** drawer — click **Restore** on an old row to flip
it current.

Use cases:

- You experimented with field locks and want to roll back
- Site started detecting after a template change — flip back to the
  pre-change FP
- Per-day FP rotation — script the restore via API

## Dual-mode toggle

`/api/fingerprint/<name>/mode` — flips the profile's active
fingerprint between desktop and mobile. The endpoint:

1. Looks for an existing matching FP in the profile's history (so
   you don't lose locked fields you set previously).
2. Falls back to fresh generation from a template of the requested
   category if no history match exists.
3. Marks the chosen FP `is_current=1`, demotes the previous current
   to history.

Useful for profiles you periodically use as both — e.g. a research
account that needs to look like a desktop on Mondays and an
iPhone on Saturdays.

## Self-test

The **Self-test** tab launches a real browser, runs a battery of
detection checks, and compares the configured fingerprint against
what the browser actually reports. The 13 checks include:

- `navigator.webdriver`
- UA + UA-CH + sec-ch-ua-platform
- `navigator.platform`, `navigator.languages`
- timezone (`Intl.DateTimeFormat().resolvedOptions()`)
- screen size + colour depth
- canvas hash + WebGL renderer
- audio fingerprint
- battery + permissions presence
- WebRTC IP leak
- font fingerprint sample
- `Date.now()` precision
- `performance.now()` precision

A "13/13" self-test pass means the patches loaded and the FP is
applied correctly. Anything less than 13/13 — drill into the failed
check and fix the underlying setting (or rebuild the patches if a
C++-level field is wrong).

## Storage

Each profile stores everything under
`profiles/<profile_name>/`:

```
profiles/profile_01/
├── Default/                   # standard Chrome user-data subdir
│   ├── Cookies                # SQLite, written via CDP or our injector
│   ├── Local Extension Settings/<id>/
│   ├── IndexedDB/
│   ├── Preferences            # JSON, patched pre-launch
│   └── ...
├── User Data/                 # additional Chrome dirs
└── (Chromium creates lots of OptGuide* / SODA* dirs over time)
```

The `OptGuide*` and `SODA*` dirs are Chromium's on-device ML models
for things like translate and dictation. Safe to ignore; they
don't affect detection.

Deleting a profile from the dashboard nukes the on-disk dir plus
every DB row referencing it.
