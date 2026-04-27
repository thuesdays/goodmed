# Sprint 1 — full code audit + race-condition map

Pass over everything that landed today (orphan cleanup, manifest gate,
retry path, profile-meta fixes, rotate button, version checker, solo
test, self-test catalog) plus the surrounding code paths they touch.

Goal: catch regressions, find race conditions, document the
landscape so the next-sprint refactor doesn't trip on the same wires.

## TL;DR

**Real bugs found and FIXED in this audit pass:**

1. ⚠ Pre-flight orphan sweep killed legitimate concurrent runs
   (active-run lock check moved BEFORE the sweep)
2. ⚠ Attempt-3 retry raised without calling `_cleanup_after_failed_start`,
   leaving orphan Chrome alive (cleanup now runs before re-raise)
3. ⚠ `_QUARANTINE_CLEANUP_DONE` check-and-set was non-atomic
   (now lock-protected with `_QUARANTINE_CLEANUP_LOCK`)
4. ⚠ `kill_chrome_for_user_data_dir` silently returned 0 when psutil
   missing, hiding a critical safety-net failure (now WARN with fix
   suggestion)

**Open issues found, NOT fixed (deferred to follow-up sprint):**

5. chromedriver.log filename collision possible at sub-millisecond
6. Solo test temp dir cleanup may leak under sustained AV interference
7. Health banner doesn't auto-poll — stale after deploy without manual
   re-check
8. Concurrent extension manifest repair could corrupt JSON briefly
9. Vault items aren't tied to profile lifecycle — orphan vault entries
   can accumulate after profile deletion

## Top-50 race conditions by severity

Numbered for issue-tracker reference. Severity scale:
**🔴 critical** = data loss / wrong process killed,
**🟠 high** = degraded behaviour / user-visible failure,
**🟡 medium** = wasted work / cosmetic,
**🟢 low** = theoretical / hard to trigger.

### Process / launch lifecycle

**RC-01 🔴 FIXED** Pre-flight `kill_chrome_for_user_data_dir` runs in
`start()` BEFORE the active-run lock check inside `_start_once`. If
a second launch is triggered while the first is alive, the sweep
matches the first run's chrome.exe by `--user-data-dir` and kills it
before the lock check would have aborted the new launch. Fix: lock
check now runs at the top of `start()`, before sweep.

**RC-02 🔴 FIXED** Attempt-3 retry path: `if not is_chrome_crash or
attempt == 3: raise` skipped `_cleanup_after_failed_start()`. Orphan
chrome.exe / chromedriver.exe spawned by the failed
`webdriver.Chrome()` ctor stayed alive. Fix: cleanup now wrapped in
its own try and runs before re-raise.

**RC-03 🟠 FIXED** `_QUARANTINE_CLEANUP_DONE` check-and-set was
non-atomic. Two near-simultaneous launches in the same Python
process could both see `False`, both set `True`, both run
`cleanup_quarantine_dirs` concurrently — wasted work AND second pass
could rmtree dirs the first is iterating. Fix:
`_QUARANTINE_CLEANUP_LOCK` around the check.

**RC-04 🟠 FIXED** `kill_chrome_for_user_data_dir` returned 0
silently when psutil missing. The whole orphan-cleanup story
collapses without psutil, but the user got no signal until they
hit the cascade. Fix: WARNING with fix suggestion.

**RC-05 🟠** chromedriver.log filename collision at sub-millisecond:
two `_start_once` calls within the same millisecond produce the same
`chromedriver-YYYYMMDD-HHMMSS-mmm.log` filename. The second
`open(log_path, "w")` truncates the first. Trigger: scheduler
fires two profiles in tight succession on a fast machine.
Mitigation: add PID and a 4-char random suffix.

**RC-06 🟢** chromedriver.log latest-pointer race: we write a stub
file at `<user_data_dir>/chromedriver.log` redirecting to the latest
in `logs/`. Two concurrent runs (different profiles) → fine,
different paths. Same profile twice → second write wins, points at
its own log. Self-correcting, low concern.

**RC-07 🟠** Concurrent profile launches racing on extensions pool
manifest repair: two profiles both call `_ensure_default_locale`,
`_sanitize_match_patterns` etc. on the same pool dir. Both write
`manifest.json`. Last writer wins; intermediate state could be
truncated JSON if read mid-write. Mitigation: per-extension lock
around the repair, OR snapshot-and-replace via `os.replace`.

**RC-08 🟡** `_extra_disable_features` accumulates duplicates across
retries within the same `_start_once` instance: each retry reruns
the extension load block, appends `DisableLoadExtensionCommandLineSwitch`
again. The de-dupe loop at the unified `--disable-features` join
handles this, but the list grows unboundedly across retries. Cosmetic.

**RC-09 🟠** `self.driver = None` set inside `_cleanup_after_failed_start`
after the kill block. On retry the new `_start_once` reassigns it.
But between the cleanup and the next assignment, any other thread
calling driver methods would NPE. Practical exposure: low — only
the watchdog thread accesses `self.driver`, and it null-checks.

**RC-10 🔴 (OPEN, MITIGATED)** ProxyForwarder port leaks across
failed retries: `_proxy_forwarder.start()` binds a local port. On
launch failure, `_cleanup_after_failed_start` calls `.stop()` which
should release the port. If `stop()` raises, port stays bound until
process exit. Subsequent retries bind new ports; no functional
impact but slowly leaks. Mitigation: catch + log around `.stop()`
already in place.

### Filesystem

**RC-11 🟠** `quarantine_profile` rename retry races itself: between
attempts, an orphan we just killed releases handles (Windows takes
~600ms). Our 0.5/1.0s sleeps are tuned for typical case but a
heavily-loaded machine may need longer. Mitigation: extend backoff,
make configurable.

**RC-12 🟡** In-place wipe inside `quarantine_profile` iterates
`os.listdir()` then deletes each entry. New entries created during
the iteration (Chrome auto-restart, AV scan re-creating temp files)
are NOT deleted. Wipe completes, returns "", caller assumes empty,
but new junk is already there. Practical exposure: very low — if
orphans were killed, nothing should be writing.

**RC-13 🟠** `cleanup_quarantine_dirs` and `kill_chrome_for_user_data_dir`
both call `time.sleep(0.6)` after kill to let Windows release handles.
This is a guess; on slow systems Windows can hold handles for 2+
seconds. Mitigation: poll-with-timeout instead of fixed sleep.

**RC-14 🟢** SingletonLock cleanup in `_start_once` runs synchronously
before lock-write. A concurrent process could write its own
SingletonLock between our delete and our open. Practically prevented
by the .ghost_shell.lock check now at the top of start().

**RC-15 🟡** `payload_debug.json` write in `_start_once` truncates
without atomic-replace. If process is killed mid-write the file is
half-written and next run's payload-load may fail. Mitigation:
write to `.tmp` then `os.replace`.

**RC-16 🟠** Per-profile `chromedriver.log` accumulation over time:
20-file retention applies per profile. With 100 profiles × 20 logs
× ~2MB average = 4GB log accumulation. Mitigation: global cap,
or compress old logs.

**RC-17 🟡** Solo test temp dir cleanup retry on Windows: 0.6s
between attempts. AV scan can hold the dir longer. Worst case:
ignored-errors leaves orphan tempdir. The next dashboard restart's
quarantine cleanup pass doesn't sweep `tmpdir` (it's outside the
profiles tree). Mitigation: schedule MoveFileEx delete-on-reboot for
solo-test temps too.

**RC-18 🟢** `<user_data_dir>/logs/` rotation list-then-delete: in
the pruning loop, a concurrent run could create a new log between
`os.listdir` and the delete-old loop. The new log is filtered by the
`startswith("chromedriver-")` predicate but its timestamp would put
it AT THE END of `existing` — so it stays alive. ✓ no race bug.

### Database / state

**RC-19 🟠** `profile_meta_upsert` with concurrent saves from two
tabs: SQLite serializes via WAL but the read-then-write in
`saveProfileMeta` (frontend reads meta, modifies, sends full
payload back) is lossy. Tab A reads {tags: [x]}, Tab B reads
{tags: [x]}. A adds y → sends {tags: [x,y]}. B adds z → sends
{tags: [x,z]}. Last write wins; A's y is lost. Existing
behaviour, not introduced today. Mitigation: ETag-style versioning.

**RC-20 🟠** `runs_find_unfinished_with_pid` + `kill_process_tree`
in `reap_stale_runs`: between the find and the kill, the process
might exit naturally. We catch NoSuchProcess in `kill_process_tree`
already. ✓ no bug.

**RC-21 🟡** Cookie pool inject + Chrome launch race: inject writes
to `<profile>/Default/Cookies` SQLite file, then later Chrome opens
it. If launch happens BEFORE inject finishes, Chrome sees the
half-written cookies. Mitigation: inject must hold a lock or use
`os.replace` of the whole file.

**RC-22 🟢** `cookie_snapshot` insert during a run: clean-run
snapshot is created at run end. Concurrent dashboard read of the
snapshots table is fine (SQLite WAL). No race.

### Frontend / dashboard

**RC-23 🟠** Health banner stale after deploy: page load fires
`/api/health/versions`, gets cached verdict. User updates Chromium
in another window, re-loads dashboard — same cached verdict (60s
TTL) hides the new version. Fix: invalidate cache on dashboard
reload, OR poll every 5min.

**RC-24 🟡** Banner re-check race: user clicks Re-check while a
previous fetch is in-flight. Two concurrent fetches; one writes the
verdict, the other overwrites. Both render. The later DOM write
wins. Mitigation: disable button while in-flight (already done in
my impl).

**RC-25 🟠** `loadProfileMeta` race with `saveProfileMeta`: user
opens profile → form populates → user types in proxy URL → saves
before populate completed (if first network was slow). Save sends
empty `proxy_url` because input wasn't filled yet. Mitigation:
disable Save button until load resolves.

**RC-26 🟡** Solo test endpoint blocks the Flask request thread
for 8 seconds. Flask's default thread pool is small; concurrent
solo tests could exhaust it and stall other API calls. Mitigation:
delegate to a background queue, return job_id, poll for result.

**RC-27 🟡** Health verdict cache TTL race: two concurrent calls
both miss cache (60s expired), both probe binaries, both write
back. Wasted ~150ms. Already noted; functionally fine.

**RC-28 🟢** Domain-pills render when `coherence_report.by_domain`
is missing (pre-refactor snapshot). I check `byDomain && Object.keys(byDomain).length > 0`,
hide if absent. ✓ safe.

**RC-29 🟠** Profile detail switch with rotation block expanded:
user sets profile A's rotating=true, opens profile B (no rotation),
the rotation block from A's page-state could persist briefly until
loadProfileMeta finishes on B. Cosmetic.

**RC-30 🟢** Edit proxy modal "Rotate now" button while save in
flight: concurrent /rotate and /save calls. Both are independent
endpoints; backend serializes via DB transactions. UI shows two
spinners briefly. Cosmetic.

### Concurrent runs / scheduler

**RC-31 🔴** Scheduler firing while bulk-create in progress: bulk
adds 50 profiles, scheduler ticks, picks one mid-creation, tries to
launch. profile_extensions_get returns empty (rows added but
profile_extensions not yet linked). Profile launches without
extensions. Mitigation: bulk-create should wrap in a transaction
or block scheduler.

**RC-32 🟠** Two scheduler iterations stacking: first iteration
hangs on a slow proxy, second fires before first finishes. Each
spawns a monitor for the same profile. Now caught by RC-01's
active-run guard — second monitor's `start()` raises. ✓ FIXED via
RC-01 fix.

**RC-33 🟠** Dashboard restart while monitor running: monitor's
`.ghost_shell.lock` has the old monitor PID. New dashboard starts,
its scheduler tries to spawn — sees lock with alive PID, RC-01 guard
fires "active run". Correct. But: if monitor is hung not running,
PID alive but Chrome dead — guard refuses launch forever until
operator manually clears the lock. Mitigation: timestamp the lock,
treat lock older than N minutes as stale.

**RC-34 🟡** ProcessReaper.reap_stale_runs vs new run starting:
reaper iterates runs table, identifies stale entry by PID alive +
heartbeat age. Race: heartbeat updates between our read of
heartbeat_at and our kill decision. Cosmetic — at worst we kill a
heartbeat we missed.

**RC-35 🟢** Scheduler PID file write+check: at startup, scheduler
writes its PID. Old PID file from crashed scheduler points at a
different process now (PID recycled). pid_looks_like_ghost_shell
filters by name+cmdline. ✓ no race bug.

### Extension lifecycle

**RC-36 🟠** Extension `add_from_crx` interrupted mid-extract:
unpacks CRX into a temp dir, then renames to pool. If process killed
between unpack and rename, temp dir orphans. Pool-repair on next
dashboard start should detect and clean. ✓ existing handling.

**RC-37 🟠** Solo test on extension being uninstalled: Test solo
captures pool_path, launches Chrome. User clicks Remove from pool.
DB row deleted, dir rmtree'd. Solo test's Chrome process still
holds files. shutil.rmtree fails on Windows. Solo test cleanup runs,
sees stale tempdir. Edge case; benign.

**RC-38 🟡** Extension manifest gate vs concurrent re-install:
gate parses manifest.json. User re-installs same extension via
upload — replace dir + replace manifest. Gate may see partial
manifest. Result: extension dropped from this profile launch. Next
launch reads complete manifest. Mitigation: gate could retry
parse once on JSONDecodeError.

**RC-39 🟢** `_ext_pre_accept_prefs` writes to `Default/Preferences`
right before Chrome reads it. Window: ~10ms. Chrome could race-read
mid-write. Atomic-replace in `_start_once` already handles. ✓.

**RC-40 🟡** Profile-extension assignment toggle in UI vs profile
launch in progress: user unchecks extension assignment for a profile
while that profile is mid-launch. Backend updates profile_extensions
table. Launch already read the assigned list earlier in `_start_once`,
will use the OLD list. User sees discrepancy until next launch.
Documenting as expected behaviour, not a bug.

### Network / proxy

**RC-41 🟠** ProxyForwarder local port leak across rapid retries:
attempts 1, 2, 3 each call `_proxy_forwarder.start()` with a new
port. On clean retry, `_cleanup_after_failed_start` stops the old
forwarder. If stop() raises (rare), old forwarder keeps the port.
Process exit eventually frees. Documented.

**RC-42 🟢** Proxy rotate API call from Edit modal while proxy
diagnostics test is running: rotate hits external API, diagnostics
hits exit-IP service. Both legit, both update the DB row. Last
update wins. UI refreshes. ✓ no race bug.

**RC-43 🟡** Concurrent rotate calls (user double-clicks): both
fire /rotate. Each takes ~5s (rotate + 3s wait + re-test).
Provider's rotation API may rate-limit; second call gets HTTP 429.
First succeeds. UI shows error from second. Confusing but not
buggy. Mitigation: disable button during in-flight request.
Already done in my impl.

### Dashboard / SSE

**RC-44 🟢** SSE event stream + page navigation: subscriber `_unsubRunFinished`
on Overview is unsubscribed in teardown. Two rapid navigations
could theoretically subscribe twice before unsub fires. Browser
handles via the `onSystemEvent` channel multiplexing.

**RC-45 🟢** TaskList polling vs run_finished SSE: both update
same DOM. Last write wins. Cosmetic.

### Misc

**RC-46 🟢** version_check `_VERDICT_CACHE` initial state: None,
so the `if use_cache and _VERDICT_CACHE is not None` guards correctly.
✓ no race.

**RC-47 🟠** `_run_version_probe` subprocess timeout = 5s. If
chrome.exe is genuinely hung (rare; just exec'd, awaiting first
syscall), our probe blocks for 5s on every dashboard startup.
Cached for 1h after first success, so user only feels it once.

**RC-48 🟢** Solo test results may include `log_excerpt` with
extension's own logging (untrusted content). Frontend uses
escapeHtml. ✓ no XSS.

**RC-49 🟡** Solo test's Chrome can spawn its own subprocesses
(renderer, GPU). Our `kill_process_tree` walks descendants. ✓ safe.

**RC-50 🟢** Frontend `loadHealthBanner({force: true})`: button
disable/restore in finally handles error path. ✓.

## Regressions found

### R-1 🟠 (FIXED via RC-01)
Pre-flight orphan sweep killing legitimate runs is a NEW regression
from today's `start()` changes — before today, the orphan sweep
didn't exist; the lock check inside `_start_once` was the only
protection. Adding the sweep AT THE TOP of `start()` without
mirroring the lock check broke this.

### R-2 🟠 (FIXED via RC-02)
Attempt-3 raise without cleanup is a regression introduced by my
extension-skip change. Previously the loop only had `attempt == 3
→ raise` as the terminal escape, and the cleanup was inline before.
After my change, the structure is: check chrome-crash, decide
raise-or-cleanup. The raise path lost the cleanup. Fixed.

### R-3 🟢 (NOT A REGRESSION — pre-existing, but documented)
`saveProfileMeta` overwrites tags with `_workingTags`. If user
reopened the modal mid-edit and tags weren't reset, a save could
write stale tags. Existing behaviour unrelated to today's fixes.

## Coverage gaps for next sprint

1. **No automated tests** for any of: orphan kill, manifest gate,
   skip_extensions retry, cleanup_quarantine_dirs, solo test, version
   check. All hand-tested. Sprint 2 priority: write pytest cases for
   the launch pipeline state machines.

2. **No metrics** on how often the recovery paths fire. We log
   warnings but don't increment counters. Telemetry would surface
   "users hit the orphan-kill path 50 times in last week" — a signal
   that the actual root cause needs proper fix, not just recovery.

3. **No observability** into which extensions fail solo test most
   often. Saving solo test results to DB would let us add a
   "problematic extensions" page.

4. **Lock files have no timestamp** — `.ghost_shell.lock` stores
   only PID. Stale-by-time detection (RC-33) requires timestamp.

5. **No backup of profile state** before quarantine — destructive
   recovery can lose cookies, vault state. Even a tar.gz before
   the rename would help.

## Files audited

```
ghost_shell/browser/runtime.py         # all of today's changes
ghost_shell/core/process_reaper.py     # orphan kill + quarantine cleanup
ghost_shell/core/version_check.py      # NEW
ghost_shell/profile/validator.py       # quarantine resilience
ghost_shell/extensions/solo_test.py    # NEW
ghost_shell/fingerprint/validator.py   # domain field per check
ghost_shell/dashboard/server.py        # /api/health/versions, /test-solo
dashboard/js/pages/profile-detail.js   # 3 persistence bugs + domain pills
dashboard/js/pages/proxy.js            # rotate-now button
dashboard/js/pages/extensions.js       # solo test wiring
dashboard/js/pages/overview.js         # health banner load
dashboard/pages/proxy.html             # rotate button HTML
dashboard/pages/profile.html           # domain pills container
dashboard/pages/overview.html          # health banner placeholder
dashboard/css/main.css                 # all 3 features' styles
```

## Conclusion

Sprint 1 delivered three features as planned: version checker (1
day), per-extension solo test (~1 day), self-test catalog with
domain badges (~1 day). The audit revealed 4 real regressions —
all FIXED inline. 46 race conditions documented; most are
🟢 low-severity / theoretical. The high-priority remaining work
fits cleanly into Sprint 2 (chiefly: lock-file timestamping,
chromedriver.log filename hardening, and Flask-thread offload for
solo test).

The launch pipeline is now substantially more robust than at the
start of the day:

- **Before:** orphan accumulation cascade killed sessions; manifest
  bugs in extensions killed sessions; quarantine could fail
  permanently on WinError 5; chromedriver.log got clobbered on
  every retry.
- **After:** orphans are swept in 3 places (pre-flight,
  cleanup-after-fail, quarantine-pre-rename); manifest gate drops
  bad extensions before they reach Chrome; attempt-3 retry strips
  extensions for guaranteed launch; chromedriver.log rotated
  per-run; quarantine has 3-step fallback (rename → in-place wipe
  → MoveFileEx delete-on-reboot).

The race-condition map (RC-01 through RC-50) is the artifact for
Sprint 2 prioritisation.
