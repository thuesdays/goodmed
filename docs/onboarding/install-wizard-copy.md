# Installer wizard — copy review

The current installer wizard pages live in
`F:\projects\ghost_shell_browser_inno\ghost_shell_installer.iss`.
This is a copy review of the user-facing strings, with suggested
replacements where the current copy is technical, ambiguous, or
unfriendly to non-developer end users.

## Welcome page

**Current** (typical Inno Setup default):
> Welcome to the Ghost Shell Anty Setup Wizard
>
> This will install Ghost Shell Anty version 0.2.0.11 on your
> computer. It is recommended that you close all other applications
> before continuing.

**Suggested**:
> Welcome to Ghost Shell Anty
>
> This installer sets up Ghost Shell Anty 0.2.0.11 — a self-hosted
> antidetect browser with a control dashboard.
>
> Before continuing, close any running browsers if you'd like
> them to pick up the new install in their search history. Other
> applications can stay open.

**Why**: the Inno default is generic and slightly alarming ("close
all other applications"). The suggested copy is specific about
what the installer is and what the "close other apps" warning
actually applies to.

## License page

**Current**:
> License Agreement
> Please read the following important information before continuing.

**Suggested** — keep header, but use a friendlier intro:
> Ghost Shell Anty is MIT-licensed. The full license text is below.
> In short: it's free to use, modify, and redistribute.
> [Continue scrolling for the formal license text.]

**Why**: MIT is short and famously permissive; flagging that
upfront removes the typical "do I have to read all this?" friction.

## Mode picker (non-fresh install)

The custom mode picker appears when a previous install is
detected. Currently:

> A previous installation was found.
>
> ⚪ Update — replace program files, keep all data
> ⚪ Repair — reinstall the same version, keep all data
> ⚪ Reinstall fresh — wipe data and reinstall

**Suggested**:
> A previous installation of Ghost Shell Anty was found at
> `%LOCALAPPDATA%\GhostShellAnty`.
>
> **What would you like to do?**
>
> ⚪ **Update** *(recommended)* — Replace program files only.
> Profiles, vault, settings, and run history are preserved.
>
> ⚪ **Repair** — Reinstall the same version. Use this if the
> current install is broken.
>
> ⚪ **Reinstall fresh** — Delete all data and start over. A
> backup of `ghost_shell.db` is created in the install folder.

**Why**: end users panic-click. Make the safe choice obvious
("recommended") and tell them which path destroys data.

## Install location page

**Current**: standard Inno location picker.

**Suggested**: keep the picker, add a help line:
> Ghost Shell Anty will install to `%LOCALAPPDATA%\GhostShellAnty`
> by default. This folder doesn't require admin permissions and
> doesn't show in Program Files. Most users should leave this as
> is.

**Why**: tells the user *why* the default location is what it is,
which preempts "should I put this in C:\Program Files instead?"
questions.

## Components page

If we have multiple components (currently we don't — full install
only), add one. For now this page can be skipped.

## Ready to install

**Current**: standard Inno page with bullet summary.

**Suggested**: same, but explicitly call out the Chromium copy:

> Ready to install. The installer will:
>
> - Copy program files (~30 MB)
> - Copy patched Chromium (~600 MB) — this is the biggest step
> - Install Python dependencies into a virtual environment
> - Create Desktop and Start menu shortcuts
> - Optionally launch the dashboard at the end
>
> The full install takes about 2-4 minutes on most machines.

**Why**: 600 MB of Chromium is the slow part; explaining it
upfront prevents "is this stuck?" anxiety during the install.

## Install progress

**Current**: standard Inno progress bar with file names.

**Suggested**: replace the per-file scroll with friendlier
high-level status:
> Copying program files…
> Copying patched Chromium (this is the slow part — about 90 seconds)…
> Installing Python dependencies…
> Setting up shortcuts…
> Finalizing…

**Why**: nontechnical users don't want to see a stream of
`chrome_win64\resources\v8_context_snapshot.bin` filenames. They
want to know which step they're on.

## Final page

**Current**:
> Setup has finished installing Ghost Shell Anty on your computer.
> The application may be launched by selecting the installed
> shortcuts.
>
> [✓] Launch dashboard now

**Suggested**:
> Done.
>
> Ghost Shell Anty installed at `%LOCALAPPDATA%\GhostShellAnty`.
> Open it from the Start menu, the Desktop shortcut, or click
> Finish below to launch it now.
>
> First steps: the [Quick Start](https://github.com/thuesdays/ghost_shell_browser/wiki/Quick-Start)
> wiki page walks you through your first profile and run in
> about 10 minutes.
>
> [✓] Launch dashboard now

**Why**: the link to the Quick Start gets users into the actual
"first run" experience without making them fish for it. The
empty wizard-finished state is the highest-conversion moment for
documentation engagement.

## Uninstaller

**Current**:
> Are you sure you want to completely remove Ghost Shell Anty
> and all of its components?

**Suggested**:
> Uninstall Ghost Shell Anty?
>
> This removes the program files, the Python virtual environment,
> and the patched Chromium binary.
>
> **Your data is preserved by default.** Profiles, the vault,
> cookie snapshots, and run history stay at:
>
> `%LOCALAPPDATA%\GhostShellAnty\data\`
>
> You can delete that folder manually if you want a fully clean
> uninstall.
>
> [Uninstall] [Cancel]

**Why**: the typical uninstaller "delete everything?" copy
terrifies people who genuinely just want to update or move
machines. Explicitly preserving data and saying where it lives
removes the friction.

## Misc

- **Replace any "Cancel" with "Cancel and exit"** on the password-
  unlock or first-launch dialogs — bare "Cancel" makes some users
  worry it's destructive.
- **The "Set as default" checkbox** on the profile detail page
  should clarify what "default" means here (target of `monitor`
  CLI, etc.).
- **The Vault unlock dialog** should add a small "Forgot your
  master password?" link that opens an explainer — not a
  recovery flow, since master is non-recoverable, but a clear
  explanation of why and what the recovery path actually is
  (delete vault data, re-add manually).
