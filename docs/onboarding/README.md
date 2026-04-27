# Onboarding artifacts

Materials for improving the first-run experience and the path from
"GitHub repo" to "running the tool successfully."

## Files in this folder

- **[demo-video-script.md](demo-video-script.md)** — 60-90 second
  voice-over script for a YouTube/dev.to demo video. Includes shot
  list, production notes, and platform-specific variants
  (Twitter, README hero GIF, Product Hunt).
- **[quick-start-gif.md](quick-start-gif.md)** — 15-second silent
  loop for the top of the README. Storyboard, ffmpeg command, and
  notes on GIF vs in-repo MP4 tradeoffs.
- **[install-wizard-copy.md](install-wizard-copy.md)** — copy
  review of the installer wizard pages (welcome, license, mode
  picker, location, ready, progress, final, uninstaller). Specific
  before/after suggestions with reasoning.
- **[installer-admin-shortcut.md](installer-admin-shortcut.md)** —
  Inno Setup `[Code]` block to flip the "Run as administrator"
  bit on the Desktop and Start Menu shortcuts post-install.
  Required for orphan-process cleanup logic to work reliably in
  edge cases (different elevation contexts, AV-spawned Chrome
  children, etc.). Plus PowerShell one-liner for users on an older
  install who don't want to reinstall.

## Why these matter

The drop-off rate from "discovers the project" to "successfully
runs it" is the single biggest leverage point for any open-source
tool. A few specifics:

- **Hero GIF in the README** — surface area: every visitor of the
  repo. A 15-second loop showing the dashboard in motion converts
  significantly better than a wall of text.
- **Demo video** — surface area: viewers of the YouTube link
  embedded in the README, in dev.to posts, in Product Hunt. The
  60-second target is deliberate — most Product Hunt and dev.to
  videos that go above 90s lose half their viewers.
- **Installer copy** — surface area: every user who downloaded
  the `.exe`. Bad copy at the install step *does* turn into bad
  reviews on alternative-finding sites. Good copy at the install
  step doesn't get noticed (which is the goal).

## What to do with these

Order of impact, highest first:

1. **Cut the hero GIF** (~1 hour for one good 15s loop). Drops
   the README's "what is this?" answer time from 30s of reading to
   ~3s of looking.
2. **Update the installer copy** (~30 min — most are 1-2 line
   changes in `ghost_shell_installer.iss`). Reduces install-time
   anxiety and confusion.
3. **Record the demo video** (~3 hours total — 1h script tweak,
   1h shooting, 1h editing/captions). Pairs with HN/dev.to post
   for the launch push.

If you only do one: the hero GIF.
