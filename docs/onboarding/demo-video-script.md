# 60-second demo video script

Target length: 60-90 seconds. Goal: someone landing on the
Releases page from a HN comment watches this and immediately
gets what the tool is and whether they want it.

Format: voice-over over screen capture of the actual dashboard
running real workflows. No talking head. No fancy motion graphics.
Show the product working.

## Shot list

| t | Visual | Voice-over |
|---|---|---|
| 0:00 | Black title card: "Ghost Shell Anty — self-hosted antidetect browser, MIT-licensed" | (silence, 1.5s) |
| 0:01 | Cut to dashboard Overview page. Cursor settles on the 24h KPIs row. | "Ghost Shell Anty is a self-hosted antidetect browser. Each profile gets its own coherent fingerprint, proxy, cookie state, and extensions." |
| 0:08 | Cut to Profiles page, showing ~10 named profiles in the table with status badges. Cursor moves to a row, clicks ▶ Start. | "Click Start — a real Chromium window opens, runs the configured flow, and exits. No JavaScript shims; the patches are in the Chromium binary itself." |
| 0:15 | Cut to opened Chromium window doing a Google search, ad detection highlighting on screen. | (continues, no new VO for ~2s) |
| 0:18 | Cut to Self-test result: 13/13 green, coherence score 91. | "The fingerprint editor scores coherence — 13 out of 13 means every check passed against the configured device template." |
| 0:25 | Cut to Fingerprint editor. Click the desktop/mobile toggle. UA, viewport, platform fields rewrite live. | "One toggle flips a profile from desktop to mobile — the entire fingerprint set is consistent because every field comes from one device template." |
| 0:32 | Cut to Extensions page, showing pool of ~12 extensions with dense IDE layout. | "Manage extensions in one shared pool. Install from the Chrome Web Store, drag a CRX, or upload an unpacked zip." |
| 0:40 | Cut to profile detail page, Extensions card. Click +Add from pool, pick MetaMask + uBlock, chips appear. | "Assign any subset to any profile. Each profile keeps its own data — wallets stay isolated." |
| 0:48 | Cut to Bulk-create dialog. Type count: 25, name pattern: client_{n:02d}, pick proxy pool. Click Create. Toast: "✓ Created 25 profiles". | "Need a fleet? Bulk-create makes 25 profiles in one dialog — round-robin proxies, optional script binding, optional cookie pool seeding." |
| 0:56 | Cut to closing card: Logo + "github.com/thuesdays/ghost_shell_browser — MIT" | "MIT-licensed. Self-hosted. Free forever. Link in description." |
| 1:00 | End. | (silence) |

## Production notes

**Recording tool**: OBS Studio (free, open-source) or any screen
capture that does 1080p60. Don't go higher — 4K demos make people
think the tool's only for power users.

**Cursor**: macOS / Windows default cursor; nothing fancy. The
cursor IS the demo's main visual storyteller — make sure its
movement is intentional and unhurried. Don't double-click rapidly;
viewers won't see what happened.

**Audio**: voice-over recorded separately in Audacity. Mic doesn't
have to be expensive — a $30 USB condenser is fine for a 60s
clip. **Read the script slowly**; the natural inclination is to
rush, but a 60s script read at 50s is unintelligible.

**Music**: optional. If you add music, it's lo-fi instrumental at
-20dB so the voice stays dominant. No vocals.

**Subtitles**: yes. Most viewers watch with sound off on the first
play. Burn them in (don't rely on YouTube auto-generation; their
captions are wrong roughly 30% of the time).

**Length**: budget 60s; aim for 70-80s in the final cut. If you're
at 100+s, cut. The Bulk-create demo can drop to a 2-second cut
showing only the final toast if you're tight.

**Where to host**:

- **YouTube**: best discovery; embed the link in README, Releases
  page, and dev.to post.
- **Asciinema** for the *terminal* parts only (e.g., a separate
  30s clip showing `python -m ghost_shell dashboard` startup if
  you want one).

## Variants for different platforms

**Twitter / X video**: cut to 30 seconds, no voice-over, big bold
on-screen captions, end card with the URL. Twitter video without
sound autoplays in feeds; sound autoplay is muted by default, so
the captions ARE the voice-over.

**README hero GIF**: the first 15 seconds of the same recording,
silent, looped. Compresses to ~3MB. Embed as an `<img>` at the
top of the README.

**Product Hunt video**: same as YouTube but with a 5-second intro
card explaining what the tool does for someone who's never heard
of "antidetect browsers" — a sentence on multi-account workflows
or privacy isolation.
