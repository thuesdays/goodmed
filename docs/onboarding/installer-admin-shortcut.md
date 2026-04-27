# Make the desktop shortcut launch as Administrator

The orphan Chrome cleanup logic in `ghost_shell/core/process_reaper.py`
benefits from elevated privileges in edge cases:

- killing chrome.exe processes spawned in a different elevation
  context (some AV / parental-control tools spawn their own Chrome
  helpers as a different user)
- scheduling stuck `*.quarantine-*` folders for delete-on-reboot via
  `MoveFileExW` — works without admin, but is reliable with admin
- `psutil.process_iter()` returning full `cmdline` info for processes
  outside the current user session
- file-handle release when a previous Chrome process was launched as
  admin and we're trying to clean up its leftover files

The cleanest way to achieve "shortcut elevates" is a one-line tweak in
the Inno Setup `[Code]` section that flips the "Run as administrator"
bit on the .lnk files post-install. Doing it on the shortcut (rather
than embedding `requireAdministrator` in the .exe manifest) means CLI
launches via `python -m ghost_shell dashboard` continue to work
without UAC prompts.

## Add to `ghost_shell_installer.iss`

Append to the existing `[Code]` section:

```pascal
{ ────────────────────────────────────────────────────────────
  SetShortcutRunAsAdmin
  Flips the "run as administrator" bit (byte 21, mask 0x20) in a
  Windows .lnk file. The bit lives in the LinkFlags field of the
  ShellLinkHeader and is set by Explorer when the user ticks
  "Advanced → Run as administrator" on a shortcut's Properties.
  We're just doing the same edit programmatically.

  Reference: https://docs.microsoft.com/en-us/openspecs/windows_protocols/ms-shllink/16cb4ca1-9339-4d0c-a68d-bf1d6cc0f943
  Section 2.1.1 LinkFlags bitfield, RunAsUser flag.
  ──────────────────────────────────────────────────────────── }
procedure SetShortcutRunAsAdmin(LinkFile: String);
var
  Stream: TFileStream;
  Buffer: Byte;
begin
  if not FileExists(LinkFile) then Exit;
  try
    Stream := TFileStream.Create(LinkFile, fmOpenReadWrite or fmShareDenyWrite);
    try
      Stream.Position := 21;
      Stream.ReadBuffer(Buffer, 1);
      Buffer := Buffer or $20;     { set RunAsUser flag }
      Stream.Position := 21;
      Stream.WriteBuffer(Buffer, 1);
    finally
      Stream.Free;
    end;
    Log('SetShortcutRunAsAdmin: flagged ' + LinkFile);
  except
    Log('SetShortcutRunAsAdmin: FAILED for ' + LinkFile + ' — ' + GetExceptionMessage);
  end;
end;
```

Then in your existing `CurStepChanged` procedure (or add one if there
isn't already), call it for both shortcut paths:

```pascal
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    SetShortcutRunAsAdmin(ExpandConstant('{commondesktop}\Ghost Shell Anty.lnk'));
    SetShortcutRunAsAdmin(ExpandConstant('{group}\Ghost Shell Anty.lnk'));
    { also any other shortcuts you create — quick-launch, taskbar pin, etc. }
  end;
end;
```

The icon names `Ghost Shell Anty.lnk` must match the `Name:` you set
in the `[Icons]` section. If yours is different (e.g. spaces vs.
underscores), adjust the strings.

## Effect for end users

- Double-click the desktop icon → UAC prompt → dashboard launches
  with admin privileges. Tray-icon process and any spawned
  monitor/scheduler children inherit the elevation.
- `python -m ghost_shell dashboard` from a non-elevated shell →
  still runs without UAC, no admin. Useful for development.
- `python -m ghost_shell dashboard` from an admin PowerShell → runs
  with admin if the shell was admin. Same as before.

The `.exe` itself stays `asInvoker` — only the SHORTCUT carries the
elevation request.

## Why not `requireAdministrator` in the .exe manifest?

Two reasons:

1. **CLI dev workflow breaks.** `python -m ghost_shell dashboard`
   would always trigger UAC, even for someone running from VS Code's
   terminal trying to debug.
2. **Scheduler subprocesses inherit weird states.** When the
   dashboard spawns a monitor or scheduler subprocess, manifest-level
   admin requirement causes Windows to consider every spawn a
   privilege transition — slows things down and can hang on
   non-interactive sessions (Terminal Services, RDP-disconnected,
   etc.).

Shortcut-level elevation is the standard Windows pattern for
"daily-use admin tools" — Visual Studio Installer does this, every
JetBrains IDE does this, etc.

## Verifying after install

1. Right-click the desktop icon → Properties → Shortcut tab → Advanced.
2. The "Run as administrator" checkbox should already be ticked.

If it's not ticked, the Pascal Script either failed (check the
installer log under `%TEMP%\Setup Log YYYY-MM-DD #NNN.txt`) or the
shortcut name in `SetShortcutRunAsAdmin()` doesn't match the actual
.lnk name on disk.

## Existing install — flag both shortcuts after the fact

For users already on an older install who don't want to reinstall,
this PowerShell one-liner does the same thing:

```powershell
Get-ChildItem "$env:USERPROFILE\Desktop\Ghost Shell Anty.lnk",
              "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Ghost Shell Anty.lnk" |
  ForEach-Object {
    $bytes = [System.IO.File]::ReadAllBytes($_.FullName)
    $bytes[21] = $bytes[21] -bor 0x20
    [System.IO.File]::WriteAllBytes($_.FullName, $bytes)
  }
```

Run from any non-elevated PowerShell — the .lnk file isn't admin-
protected, only the apps the shortcut launches need elevation.
