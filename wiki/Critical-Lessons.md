# Critical Lessons

Engineering war-stories worth knowing before you hack on the code.
Each one cost real time to track down — most of a day in some
cases. Documented here so the next person doesn't repeat them.

## 1. `parse_manifest` regex was eating valid JSON

**Symptom**: Some real-world wallet extensions imported with
mangled `manifest.json` — fields just gone, like `content_scripts`
chopped, `host_permissions` truncated. No error, no warning, just
silently broken extensions that wouldn't load.

**Cause**: `parse_manifest` had a comment-stripping regex
`/\*[\s\S]*?\*/` to remove C-style comments before `json.loads()`.
But Chrome doesn't accept comments in manifests anyway, and the
regex was non-anchored — it matched across string boundaries. A
manifest with `"matches": ["http://Web/*"]` followed later by
`"https://*/*"` produced a regex span starting at `Web/*` and
ending at `*/`, ~250 characters of valid JSON in between gone.

**Fix**: Comment stripping removed entirely. Chrome rejects
manifests with comments anyway, so we let the parser hand back the
raw error if someone uploads a manifest with `//` lines.

**Lesson**: never run a regex pre-pass on data you'll then parse
properly. Either hand the raw bytes to the proper parser, or
preserve the original perfectly and operate on the parsed tree.

## 2. `default_locale` is a structural check, not a string check

**Symptom**: Extensions with a `_locales/` folder but no
`default_locale` in the manifest failed to install. Chrome's error
was unclear ("Default locale not found" — but the folder was
right there).

**Cause**: Chrome treats `default_locale` as **required iff a
`_locales/` folder exists**. The two have to match — folder
present + field absent = error. Many real-world extensions ship
`_locales/` and assume the lookup is automatic.

**Fix**: `_ensure_required_fields()` now scans for `_locales/`
on import; if found and `default_locale` missing, defaults it to
`en` (or the first locale folder if no `en/`).

**Lesson**: when the platform's "required field" check fires, look
for a *structural* trigger (a folder, a sibling file) — not just
the field's presence in the JSON.

## 3. `pinned_actions` vs `pinned_extensions` vs `toolbar`

**Symptom**: Auto-pin code wrote `extensions.pinned_extensions` to
`Default/Preferences`, but assigned extensions still showed up
under the puzzle-piece menu instead of on the toolbar in our test
Chrome 149 build.

**Cause**: Chrome 138+ uses `extensions.pinned_actions` as the
authoritative key. `pinned_extensions` is the **transitional** key
from Chrome 91-137; older versions used a legacy
`extensions.toolbar`. We were writing the wrong one for our
version.

**Fix**: Write **all three** keys with the same ID list. Chrome
silently ignores the keys it doesn't recognize. Code now survives
any Chrome 88-149.

**Lesson**: Chrome's preference keys mutate across versions. Look
at a real preference file produced by *your* Chrome version
(after a manual pin) before assuming a key name from older docs.

## 4. Multiple `--disable-features` flags don't merge

**Symptom**: We passed `--disable-features=Foo` from one config and
`--disable-features=Bar` from another (different layers). Chrome
only honoured the second one.

**Cause**: Chrome doesn't merge repeated `--disable-features`
flags — the later one overwrites. Same for `--enable-features`,
`--enable-blink-features`, etc.

**Fix**: A central join in `runtime.py` collects all
disable-features sources and emits a single comma-joined flag:
`--disable-features=Foo,Bar`.

**Lesson**: any Chrome flag that takes a comma-separated list must
be assembled centrally. Don't pass it from multiple places.

## 5. `Edit` tool corruption on Windows mounts

**Symptom**: After a series of file edits, suddenly the file's
last several thousand lines were missing. Editor still shows the
full file open in another window — but the on-disk content is
truncated.

**Cause**: Some interaction between the Cowork file-edit tool, the
Windows path layer, and certain file-write patterns occasionally
truncates writes on save. Reproducible enough to be worth a
recipe.

**Fix recipe** (in `edit_tool_corruption.md` if memory persisted):

1. **Don't panic** — the file is recoverable from the editor
   buffer if it's still open in VS Code / your editor.
2. If the editor's also lost it, recover from git:
   `git show HEAD:<path> > <path>` — gets you the last committed
   version.
3. To prevent recurrence, after every multi-edit session: open
   the file in VS Code and **re-save** (Ctrl+S) before closing
   the session. That round-trips through the editor's normal
   write path.

**Lesson**: trust but verify. After big batches of edits, sanity-
check file sizes against expected line counts.

## 6. Inno Setup AppId asymmetry

**Symptom**: Update flow on the installer didn't detect the prior
install — wizard showed "fresh install" mode every time, even
when the previous version was registered.

**Cause**: Inno Setup's AppId is internally enclosed in double
braces, but the resulting `Uninstall\<AppId>_is1` registry key
is sometimes single-braced and sometimes double-braced depending
on the host's Inno version + locale.

**Fix**: `FindExistingInstall()` in the .iss tries three subkey
forms — the single-brace form, double-brace, and unbraced —
falling back through them. First match wins.

**Lesson**: registry-key naming around AppIds is not stable
across Inno versions. Always probe multiple forms.

## 7. `commercial_inflate` tab leak

**Symptom**: Long-running `foreach_ad` flows would slowly accumulate
tabs over the course of an hour — by run 100 you'd have 80+ tabs
open and Chrome was eating multi-GB of RAM.

**Cause**: Some `click_ad` targets opened popups (`window.open`)
that we never closed. Each iteration of the loop added one or
two leaked tabs.

**Fix**: A new `_close_extra_tabs()` helper runs between
iterations of `foreach_ad` and once at the end of the script —
finds any tab that isn't the original SERP tab, closes it.

**Lesson**: long-running browser automation needs a tab-leak
cleanup step. Don't trust that `click_*` always closes what it
opens.

## 8. `psutil` bottleneck on dashboard startup

**Symptom**: Dashboard took 15-25 seconds to come up on first
HTTP request after `python -m ghost_shell dashboard`. Annoying
during dev cycles.

**Cause**: `cleanup_stale_runs()` was iterating every PID on the
system via `psutil.process_iter()` to mark stuck `runs` rows as
failed. On a Windows box with many processes, this was the
bottleneck.

**Fix**: Moved `cleanup_stale_runs()` into a daemon thread that
runs in the background after the Flask app is already serving.
Startup is now < 2s; cleanup completes asynchronously and writes
its results when ready.

**Lesson**: any startup task that scans system state is a
candidate for a background daemon. Server availability beats
correctness-on-first-request for non-critical bookkeeping.

## 9. `{item}` template eager-clearing in nested foreach

**Symptom**: Variables set via `set_var` outside a `foreach_ad`
loop disappeared inside the loop body.

**Cause**: The runner cleared all template variables when entering
a nested step's `params` resolution, including ones set in the
outer scope.

**Fix**: Template clearing now only clears keys whose names start
with `ad.` or `item.` — the loop-local variables. Outer-scope
variables persist.

**Lesson**: scope rules for template variables need explicit
naming conventions; otherwise nested loops will silently
corrupt outer state.

## 10. Frontend reading `meta.meta?.use_script_on_launch` instead of `meta?.use_script_on_launch`

**Symptom**: The "use script on launch" checkbox in the profile
detail page was always unchecked, regardless of the saved value.

**Cause**: Two bugs at once. (a) Frontend was destructuring
`meta.meta` instead of `meta`. (b) Backend's whitelist of
allowed `meta` keys excluded `use_script_on_launch` so it never
got saved either.

**Fix**: One-character frontend fix + add the field to the
backend whitelist.

**Lesson**: when a checkbox "doesn't work", check both the read
path and the write path. They fail independently.

---

If you hit something painful that isn't in this list, please open
an issue with reproduction steps — adding to this list is one of
the highest-leverage doc contributions.
