# FAQ

## What is this for?

Self-hosted antidetect browser with a Flask control dashboard.
Useful for:

- **Privacy isolation** — keep work / personal / research browsing
  in separate fingerprints so cross-profile tracking can't link
  them
- **Multi-account management** — agencies running multiple client
  social-media accounts on platforms whose ToS allow multi-account
- **QA and accessibility automation** — drive a real browser through
  a test matrix of devices without owning every device
- **Fingerprint and anti-detection research** — mature, debuggable
  surface for studying how detection works
- **Ad-ops monitoring of *your own* campaigns** — competitive
  intelligence on the SERP positions and ad copy *you* are
  paying for

It's **not** intended for click-fraud against advertisers, sybil
farming on protocols that prohibit it, account farming on platforms
that prohibit multi-account, or evading another platform's
anti-fraud systems for commercial gain at their users' expense.
See the README's
[License & Acceptable Use](https://github.com/thuesdays/ghost_shell_browser#license--acceptable-use)
section.

## How is it different from `<commercial antidetect browser>`?

**Free and open-source.** No per-profile fees, no cloud
subscription, no unlock tiers. MIT licensed.

**Self-hosted.** Your fingerprints, cookies, and proxy credentials
never leave your machine. No telemetry. No mandatory cloud sync.

**C++-level patches**, not JavaScript shims. The most-used commercial
browsers patch detection signals via JS injected into every page.
Modern detection scripts catch that. Our patches are in the binary
itself — patched Chromium 149 — so the C++ surface lies straight to
JS.

**Inspectable.** All of the magic is Python plus C++ patches you
can read. No proprietary anti-detection engine you have to trust.

**Smaller community.** Commercial offerings have more device
templates, more polished UX, more onboarding hand-holding,
faster turnaround on detection-rule updates. If you need any of
those more than you need free-and-inspectable, a commercial
product is probably the right fit.

## Why Chromium 149 specifically?

The patches are tied to that version's source layout. A new patched
build is needed every Chromium release; we've automated most of it
(see `MACOS_BUILD.md` for the manual procedure). Released versions
of Ghost Shell ship with the matching binary.

We rebase to a new Chromium version about every 4-6 weeks once the
detection rules around the new release stabilize. Old versions
keep working — they just stop receiving upstream security fixes.

## Why a desktop dashboard and not a web service?

The dashboard runs on `http://127.0.0.1:5000` — local-only by
default. We don't want to be a service that holds your proxies,
your fingerprints, your cookies, your vault contents. Self-hosted
means *your machine, your data*.

You can expose the dashboard to your LAN (or the internet) via
the standard Flask hosting story — but the default is
loopback-only and you have to opt in.

## How does the vault work?

Master password → PBKDF2-HMAC-SHA256 (200k iterations) + salt →
32-byte Fernet key. Fernet is AES-128-CBC + HMAC-SHA256, so each
encrypted blob has integrity + confidentiality.

The 32-byte key lives in **process memory only** — lost on restart.
You re-unlock the vault at the start of each dashboard session.
The DB only sees ciphertext.

A small "verifier" token is stored in `config_kv` so we can confirm
your master password without decrypting any real secret. If
verifier matches, we proceed; if not, we abort before touching
any vault data.

If you forget your master, vault items are unrecoverable — that's
the design. Pick something memorable.

## Why one shared extensions pool instead of per-profile copies?

Disk space, update consistency, and code-surface uniformity. A
serious extension is 5-30 MB; 50 profiles of duplicated bytes adds
up to gigabytes. With one pool, an update reaches every profile
on its next launch automatically.

Per-profile extension *data* (cookies, IndexedDB, login state) is
fully isolated by user-data-dir — see
[Architecture → Extension model](Architecture.md#extension-model).

## Can I use this on Mac?

The Python layer is fully cross-platform. The patched Chromium
binary is shipped pre-built only for Windows.

Three macOS options:

1. **Dashboard-only** — read/edit config, inspect history, manage
   the vault, run proxy diagnostics. No actual browser launches.
2. **SSH bridge** — run the dashboard from your Mac through an SSH
   tunnel to a Windows runner box. Best for solo operators.
3. **Native build** — build patched Chromium from source on Mac.
   Takes 1-4 hours first time. See
   [`MACOS_BUILD.md`](https://github.com/thuesdays/ghost_shell_browser/blob/main/MACOS_BUILD.md).

## Can I use this on Linux?

Same status as Mac — Python layer is cross-platform, you'd need to
build Chromium yourself. The build procedure is essentially
identical to Mac with `target_os = "linux"` in `args.gn`. We
haven't written a dedicated `LINUX_BUILD.md` yet — community PRs
welcome.

## Why is the Chromium binary not in git?

GitHub's per-file limit is 100 MB; our `chrome.exe` plus its
sibling `.dll`s and `.pak` resources weighs ~400 MB. `chrome_win64\`
is `.gitignore`-d.

For source-only checkouts, run `scripts\download_chromium.ps1` to
pull the matching release asset from GitHub Releases. End users
don't see this — the prebuilt installer ships with the binary
included.

## How do I contribute?

See [Home → Contributing](Home.md#contributing). Easy first PRs:
new device template, new warmup preset, dashboard CSS polish,
issue with reproduction steps. For larger architectural changes,
open an issue first.

## How can I support the project?

The README's
[Support the project](https://github.com/thuesdays/ghost_shell_browser#support-the-project)
section has crypto donation addresses. Other ways: star the repo,
write a blog post about something interesting you built on top
of it, or just file good bug reports — they're more valuable than
they look.
