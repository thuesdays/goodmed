# Sprint 2 — Profile workflow audit (UI ↔ backend)

Deep audit of the four profile lifecycle flows — Create, Edit, Delete,
Launch — and every interaction point between dashboard UI, Flask
endpoints, DB layer, filesystem, scheduler, and monitor subprocesses.

Severity legend:
**🔴 critical** = data loss / wrong process killed / DB corruption
**🟠 high** = degraded behaviour / user-visible failure
**🟡 medium** = wasted work / cosmetic
**🟢 low** = theoretical / hard to trigger

Status:
**✅ fixed** in today's work
**⚠ partial** — partially mitigated, still has open edge case
**❌ open** — not addressed

## Workflow map

```
                      CREATE
   ┌──────────┐                  ┌────────────────┐
   │ Profiles │──"+ New" form──▶│ POST /profiles │
   │   page   │                  └────────┬───────┘
   └──────────┘                           │
        │                                 ▼
        │  "⚡ Bulk"                ┌─────────────────┐
        └─────────────────────────▶│POST /profiles/  │
                                   │  bulk           │
                                   └────────┬────────┘
                                            │
                                            ▼
                              DB insert (profiles row, ready_at=NULL)
                              + fingerprint_save
                              + profile_meta_upsert (tags)
                              + proxy_assign_to_profile
                              + script_assign_to_profile
                              + profiles/<name>/ mkdir
                              + cookie_pool inject (browserless)
                              + profile_mark_ready (ready_at=NOW)


                       EDIT
   ┌────────────┐
   │  Profile   │──"Save proxy" → POST /profiles/<n>/meta
   │   detail   │──"Save tags"  → POST /profiles/<n>/meta
   │   page     │──"Set proxy"  → POST /profiles/<n>/proxy
   │            │──"Set script" → POST /profiles/<n>/script
   │            │──"Toggle ext" → POST /profiles/<n>/extensions
   │            │──"Regen FP"   → POST /profiles/<n>/fingerprint/regenerate
   │            │──"Use script" → POST /profiles/<n>/meta (use_script_on_launch)
   └────────────┘


                      DELETE
   "Danger zone" → confirm dialog → DELETE /profiles/<n>
   ├── refuse 409 if RUNNER_POOL.is_profile_running(name)
   ├── shutil.rmtree(profiles/<n>)
   ├── DELETE FROM events/selfchecks/fingerprints WHERE profile_name=?
   ├── profile_meta_delete (the profiles table row + meta keys)
   ├── reassign browser.profile_name if pointed at deleted
   ├── runs/competitors/action_events stay (run history is preserved)
   └── vault_items: NOT cascaded (orphan vault entries persist)


                      LAUNCH
   ┌──────────┐
   │ Run btn  │──POST /api/runs/start──┐
   │ on page  │                        │
   └──────────┘                        ▼
                              Flask main thread:
                              ensure_profile_ready_to_launch ──→ 409 if not ready
                              spawn monitor subprocess
                                       │
                                       ▼
                              ghost_shell.main → __main__
                              ┌──────────────────┐
                              │ browser/runtime  │
                              │  start():        │
                              │   • active-run   │ ── 🚫 if heartbeat fresh
                              │     guard        │
                              │   • orphan sweep │
                              │   • quarantine   │
                              │     cleanup      │
                              │   • validator    │
                              │   • 3-attempt    │
                              │     retry loop   │
                              │     (skip_ext on │
                              │      attempt 3)  │
                              │  _start_once():  │
                              │   • payload      │
                              │   • lock write + │
                              │     heartbeat    │
                              │   • prefs merge  │
                              │   • options      │
                              │   • ext gate     │
                              │   • version chk  │
                              │   • selenium     │
                              │     ctor         │
                              └──────────────────┘
```

---

# Race condition catalogue (70 items)

## CREATE-flow (PR-01 .. PR-15)

**PR-01 🔴 ✅** Bulk-create + scheduler tick race: scheduler picks a
half-created profile, launches against missing extensions/proxy.
Fixed via `profile.ready_at` column + `profile_mark_ready()` after
all 7 setup steps + `ensure_profile_ready_to_launch` guard in
scheduler. Sprint 2.4.

**PR-02 🟠 ❌** Bulk-create cancel mid-creation (browser tab closes,
server killed): half-created profile rows remain with `ready_at IS
NULL` permanently. No GC. Recommendation: dashboard startup hook
should sweep `WHERE ready_at IS NULL AND created_at < NOW - 1h`
and either retry the setup pipeline or auto-delete.

**PR-03 🟠 ❌** Two browser tabs submit the same `/api/profiles/bulk`
simultaneously with overlapping name patterns (`prefix=acme_`).
Both pre-fetch `profiles_list()`, neither sees the other's
in-flight inserts, both pick `acme_001` as next free → second
INSERT fails with `UNIQUE constraint failed`. Failed entries
recorded in `failed: [...]` array, but UX is confusing. Fix:
SELECT FOR UPDATE-style serialisation, or accept duplicate-name
errors as expected.

**PR-04 🟡 ❌** Single-create `api_profile_create` does NOT call
`profile_mark_ready()` — newly-created single profiles get
`ready_at = NULL` and are skipped by scheduler until manually
launched once. Fix: call `db.profile_mark_ready(name)` at the end
of single-create after all setup completes. Should be a 2-line fix.

**PR-05 🟠 ❌** Profile dir creation fails (permission denied, disk
full): bulk-create reaches step 6 (mkdir) and raises. Profile is
half-created (DB row + fingerprint exists, no dir, no
ready_at). Recovery: orphan-row sweep (PR-02 fix would handle).

**PR-06 🟠 ❌** Cookie pool inject reads donor's cookies.json while
the donor's own next-run write is happening: read partial-write
SQLite. Probably benign — SQLite WAL handles read consistency —
but the inject module reads via raw file I/O in browserless
mode. Fix: snapshot donor `cookies.json` to temp before inject,
or use `os.replace` in donor's writer.

**PR-07 🟡 ❌** Bulk-create logging is per-profile-line, no progress
indicator on the dashboard during the 10-100s wait. Frontend
modal shows "Creating…" with no progress bar. Fix: stream
progress via SSE (`profile_created` events).

**PR-08 🟢 ❌** Race in `idx = start_index` collision-skip loop:
between `if name in all_existing: continue` and the actual
INSERT, another writer (single-create) could grab that name.
Loop's idempotent retry still works because INSERT raises and
the `failed` list captures it. Cosmetic only.

**PR-09 🟠 ❌** Fingerprint generation fails (template missing /
deleted): bulk-create per-profile try/except logs the failure
and continues. Profile row is NOT inserted (good), but partial
side-effects (fingerprint payload validated but not saved) may
leave inconsistent state on disk in `profiles/<name>/` if the
mkdir already ran. Fix: order setup steps so disk effects come
last.

**PR-10 🟡 ❌** Cookie pool inject queues `pending_restore.<profile>`
in `config_kv`. Multiple bulk-creates targeting the same profile
name (theoretical) overwrite each other's pending restore.
Practically prevented by name-collision detection above.

**PR-11 🟢 ❌** Cookie pool inject can't find a matching donor:
proceeds without seeded cookies. Profile is created but expects
seeded session — first run shows as fresh-blank-profile to the
target site. Cosmetic; user can manually warm later.

**PR-12 🟠 ❌** Bulk-create with `script_id` pointing at a script
that gets deleted between selection and INSERT: FK constraint
fails (scripts.id is referenced ON DELETE CASCADE). Profile row
not created; `failed[]` captures. Fix: validate script exists
inside the per-profile transaction.

**PR-13 🟠 ❌** Bulk-create proxy_pool round-robin: if proxy
pool[i] is deleted concurrently, INSERT fails for that profile.
Same as PR-12, captured in `failed[]`.

**PR-14 🟡 ❌** Bulk-create `tags` value is mutable list — caller
could pass `["a", "b"]` then later modify the same list. We
serialize on first read; subsequent profiles see same tag set.
Defensive but worth noting: take a deep copy at endpoint entry.

**PR-15 🟢 ❌** `profiles_list()` joins on run history → during
bulk-create's name collision check, the existing-name set
includes deleted-profile tombstones. We auto-bump past these
(harmless), but it slows large bulk-creates. Cosmetic.

---

## EDIT-flow (PR-16 .. PR-30)

**PR-16 🔴 ✅** `proxy_is_rotating` checkbox not persisting (Sprint 1.4).
Frontend dropped the field on save AND read meta.X wrong on load.
Fixed in profile-detail.js.

**PR-17 🔴 ✅** `rotation_api_key` silently dropped on save / not
restored on load. Same shape as PR-16. Fixed.

**PR-18 🔴 ✅** `meta.meta?.use_script_on_launch` residual lookup (was
always undefined, toggle always rendered OFF after reload). Fixed.

**PR-19 🟠 ❌** `saveProfileMeta` pre-read race (RC-25 from sprint-1):
user opens form before `loadProfileMeta` resolves, types, saves.
Save sends empty `proxy_url` because input wasn't filled. Fix:
disable Save button until load resolves. **Targeted today.**

**PR-20 🟠 ❌** Two-tab simultaneous edit: tab A sets tags=[x,y],
tab B sets tags=[x,z]. Last write wins; tab A's y is lost.
Existing behaviour, no version control. Fix: ETag / If-Match
support on POST.

**PR-21 🟠 ❌** Proxy form: rotating=true but rotation_url empty.
Backend accepts the half-config. Next launch sees rotating=true
but rotation API call has no URL → captcha-recovery path fails
silently. Fix: backend validation `if proxy_is_rotating: require
rotation_api_url`.

**PR-22 🟡 ❌** Tag deduplication is case-sensitive: `["VIP",
"vip"]` saves both. Fix: normalize on save (lowercase, trim).

**PR-23 🟡 ❌** Long tag list (50+ tags): no UI scrollbar, chips
overflow; backend stores TEXT JSON of any length. Fix: cap at
20 tags, surface in UI.

**PR-24 🟠 ❌** Assigned script gets deleted: profile keeps the
`script_id` pointing at gone row. Launch reads stale ID, falls
back to default script. FK is `ON DELETE CASCADE` (database.py:301)
— actually deleting the script row should null the profile's
script_id. Verify via integration test.

**PR-25 🔴 ✅** Edit extensions: profile_extensions row points at a
pool dir that's been deleted. Already handled by manifest gate
in `runtime.py` — drops bad ones. Fixed in Sprint 1.

**PR-26 🟠 ❌** Edit fingerprint via "Quick regenerate" while a
monitor is running for that profile: new FP saved to DB with
`is_current=1`, but live Chrome still has old FP loaded. User
sees "regen succeeded" but next page in current run still uses
old. Cosmetic for short runs; surprising for long-running ones.
Fix: warn / refuse regen while running.

**PR-27 🟠 ❌** Toggle `use_script_on_launch` while scheduler tick
is mid-flight for this profile: scheduler reads stale value.
Race window is small (<1s). Cosmetic.

**PR-28 🔴 ✅** Edit profile while delete is in progress: delete is
synchronous in Flask request thread, holds DB transaction.
SQLite serialises. Edit waits, gets a 404 on `profile_meta_get`
(row gone), surfaces error to user. Existing behaviour, OK.

**PR-29 🟠 ❌** Frontend POSTs `meta` payload while another tab's
delete just landed: 404 on the POST. UI shows raw error. Fix:
detect 404 and surface "profile no longer exists, refresh page".

**PR-30 🟢 ❌** Concurrent saves of the same field from two tabs
within 50ms: SQLite serialises the writes. Order is non-
deterministic. Cosmetic.

---

## DELETE-flow (PR-31 .. PR-42)

**PR-31 🔴 ⚠** Delete profile while monitor running: returns 409 if
`RUNNER_POOL.is_profile_running(name)` returns True. BUT —
RUNNER_POOL is dashboard-process-local; if monitor was spawned
by SCHEDULER subprocess, the dashboard's RUNNER_POOL doesn't see
it. Result: delete succeeds, scheduler-spawned monitor keeps
running against a profile that no longer exists, writes to
deleted dir / DB row. Fix: also check `runs.heartbeat_at`
freshness OR use `ensure_no_live_run_for_profile`-style check
(DB-level, sees all runs).

**PR-32 🔴 ❌** Delete dir while orphan Chrome holds a file:
`shutil.rmtree(..., ignore_errors=True)` swallows the failure.
Result: DB rows gone but `profiles/<name>/` partially remains.
Next single-create of same name sees half-deleted dir, weird
crashes. Fix: kill orphans first (use
`kill_chrome_for_user_data_dir`), then rmtree, then verify dir
gone — schedule MoveFileEx delete-on-reboot if not.

**PR-33 🔴 ❌** Vault items linked to deleted profile: `vault_items`
table has `profile_name` index but **no FK** (database.py grep
confirmed). Vault entries persist as orphans. Fix: explicit
DELETE in delete handler, OR add FK with CASCADE.

**PR-34 🟠 ❌** profile_extensions FK: `ON DELETE CASCADE` exists
on `extension_id` reference but NOT on `profile_name`. Deleting
a profile leaves orphan profile_extensions rows. Fix: explicit
DELETE in handler, OR add FK constraint.

**PR-35 🟠 ❌** cookie_snapshots have `profile_name` column but no
FK / no cleanup in delete handler. Snapshots from deleted
profiles persist forever. Fix: explicit DELETE OR cookie pool
GC pass.

**PR-36 🟠 ❌** scheduler config (`config_kv` keys like
`scheduler.profile_names`) may include the deleted profile name.
Scheduler tries to fire it, finds no row, logs warning and skips.
Cosmetic but pollutes scheduler logs. Fix: post-delete sweep of
scheduler config keys.

**PR-37 🟠 ❌** profile_groups membership: `profile_group_members`
table has FK on `group_id` ON DELETE CASCADE, but
`profile_name` is plain TEXT. Group membership references a
deleted profile name forever. Fix: explicit DELETE.

**PR-38 🟠 ❌** Active profile reassignment after delete: handler
picks `profiles_list()[0]` excluding the deleted name. If list
becomes empty, sets `browser.profile_name = None` (good).
Cosmetic: the user gets reassigned to a random profile silently.
UX improvement: prompt user.

**PR-39 🟡 ❌** Quarantined version of the profile (`profiles/<name>.quarantine-<ts>/`)
is NOT deleted by the handler — only `profiles/<name>/` is.
Stale quarantines accumulate. Mitigated by
`cleanup_quarantine_dirs` once-per-process sweep.

**PR-40 🟡 ❌** Delete double-click: UI fires DELETE twice. Second
returns 404. Cosmetic if frontend handles 404 gracefully (it
should — toast "Profile already deleted"). Verify.

**PR-41 🟢 ❌** Delete during scheduler iteration's `profiles_list()`
read: scheduler iterates a stale list snapshot, hits 404 on launch.
Logs warning, skips. Existing behaviour, OK.

**PR-42 🟠 ❌** Pending cookie inject (`session.pending_restore.<profile>`)
NOT cleared on delete. Next single-create with same name will
get an unexpected cookie restore from the prior incarnation.
Fix: explicit `config_kv DELETE WHERE key LIKE 'session.pending_restore.<name>'`
in delete handler.

---

## LAUNCH-flow (PR-43 .. PR-65)

**PR-43 🔴 ✅** Pre-flight orphan sweep killing legitimate concurrent
run (RC-01). Lock-check moved before sweep. Fixed Sprint 1.4.

**PR-44 🔴 ✅** Attempt-3 raise without cleanup_after_failed_start
leaves orphans (RC-02). Cleanup now runs before re-raise.
Fixed Sprint 1.4.

**PR-45 🔴 ✅** `_QUARANTINE_CLEANUP_DONE` non-atomic (RC-03).
Lock-protected. Fixed Sprint 1.4.

**PR-46 🟠 ✅** `kill_chrome_for_user_data_dir` silent fail without
psutil (RC-04). Now WARN. Fixed Sprint 1.4.

**PR-47 🟠 ✅** Hung monitor used to lock profile forever (RC-33).
Lock heartbeat + 180s stale detection. Fixed Sprint 2.1.

**PR-48 🔴 ❌ NEW DISCOVERY** `runtime.py:close()` was silently
truncated to no-op for 4 commits (28e6683 → v0.2.0.12). Fixed
in Sprint 2 by restoring full body from initial commit.
**Impact**: `driver.quit()`, watchdog stop, exited_cleanly stamp,
proxy forwarder stop, log handler detach — all silently
no-op'd in production. Tab-pile-up bug, port leak, log handler
duplication on scheduler — all enabled by this regression.

**PR-49 🟠 ❌** Three concurrent launches of three different profiles
on one machine: each spawns chromedriver listening on a random
port, each starts Chrome. Resource contention; ~1.5GB RAM per
Chrome. Fix: scheduler concurrency cap. Existing config but
default cap may be too high for low-RAM hosts.

**PR-50 🟠 ❌** Manifest validation gate parses each manifest on every
launch — duplicated I/O when the same extension is assigned to
many profiles. Cosmetic, ~20ms per ext. Fix: cache parsed
manifests by mtime.

**PR-51 🟠 ✅** All extensions broken (every manifest invalid):
attempt 3's `skip_extensions=True` strips them all → minimal
viable launch succeeds. Fixed Sprint 1.2 + 1.4.

**PR-52 🟠 ❌** ProxyForwarder local port binding fails (port already
in use): `_proxy_forwarder.start()` raises. Existing handling
treats as launch failure → retry. On retry, picks new random
port. Effective recovery; logs warning.

**PR-53 🔴 ❌** C++ payload broken (env-var `GHOST_SHELL_SKIP_PAYLOAD=1`
is the only workaround): retry loop fails all 3 attempts
identically. No automatic fallback. Fix: on attempt 3 failure
when extensions already stripped, set
`GHOST_SHELL_SKIP_PAYLOAD=1` in env for a 4th attempt.

**PR-54 🟠 ❌** Profile launches with no fingerprint at all
(just-created, never run): `_start_once` calls `validate()` on
None — defensive code in validator handles. Score = None,
grade = "unknown". Profile launches but coherence isn't tracked
until first manual save.

**PR-55 🟠 ❌** Cookies inject queue (`session.pending_restore.<profile>`)
never clears on launch failure: next launch tries to restore the
same snapshot, fails again, never moves on. Fix: clear pending
restore on launch failure.

**PR-56 🟠 ⚠** Two scheduler ticks fire same profile near-simultaneously:
- Tick A spawns monitor, gets DB run row.
- Tick B's `ensure_profile_ready_to_launch` sees alive run from A,
  refuses. ✓ guard works.
- BUT: between scheduler's check and monitor's actual lock-write,
  there's a 0.5-2s window. If two schedulers (dashboard+CLI) both
  check simultaneously, both might pass the check, both spawn.
  Active-run guard inside `start()` catches the second on lock-
  write attempt. ✓ second tier catches it.

**PR-57 🟠 ❌** Watchdog kills chromedriver mid-launch: launching
takes 5-8s; watchdog fires every 30s normally. If user's
launch is slow due to AV scan, watchdog could fire in the
middle. Existing watchdog timeout is generous; still possible.
Fix: watchdog should not engage until first navigation succeeds.

**PR-58 🟠 ❌** Payload write to disk fails (read-only filesystem,
disk full): `_start_once` writes `payload_debug.json` without
atomic-replace. Failure leaves half-file or no file. Next read
fails. Fix: write to .tmp + os.replace.

**PR-59 🟠 ❌** profile_extensions row points at deleted pool dir:
manifest gate skips with warning. ✓ handled. No further action.

**PR-60 🔴 ❌ NEW** Frontend "Run profile" button → POST /api/runs/start
returns 200 → user navigates away → run starts but UI doesn't
show progress on next page. SSE event `run_started` fires; if
user's next page doesn't subscribe to it (e.g., they're on
Settings), no visible feedback. User clicks again, gets 409.
Fix: make Run button persistent (show running state across
pages) OR fire toast on run_started globally.

**PR-61 🟠 ❌** chromedriver/Chrome version mismatch detection at
launch is WARN-only. Launch proceeds, fails. Fix: turn into
hard refusal at attempt 1 — saves the user 8 seconds and a
quarantine attempt.

**PR-62 🟠 ❌** chromedriver.log filename collision sub-millisecond
(RC-05): two launches in same ms produce same filename. Second
write truncates first. **Targeted today.**

**PR-63 🟠 ❌** Concurrent extension manifest repair race (RC-07):
two profiles both repair manifest.json of the same shared pool
extension simultaneously. Last writer wins; intermediate state
could be truncated JSON if read mid-write. **Targeted today.**

**PR-64 🟢 ❌** `_extra_disable_features` accumulates across
retries (sprint-1 audit RC-08): de-dupe handles, but list
grows. Cosmetic.

**PR-65 🟡 ❌** Run never completes — heartbeat updates forever
(monitor stuck in infinite script loop). Lock heartbeat keeps
fresh. From scheduler's POV, profile stays "live forever".
Fix: also tie lock heartbeat to recent activity (last
HTTP-call from selenium <60s) — currently only tied to wall
clock.

---

## CROSS-PROCESS / OBSERVABILITY (PR-66 .. PR-70)

**PR-66 🟠 ❌** Dashboard restart while monitor running: dashboard's
in-memory `RUNNER_POOL` empties. Monitor still runs. New
delete request sees `is_profile_running=False` (RUNNER_POOL is
empty), allows delete → ⚠ disaster. Fix: RUNNER_POOL should
hydrate from `runs WHERE finished_at IS NULL` on startup.
**Or**: PR-31's fix (DB-level liveness check) replaces this.

**PR-67 🟠 ❌** Monitor PID recycled to unrelated process on long
uptime: `pid_looks_like_ghost_shell` filters by name+cmdline,
should reject the recycled one as not-ghost-shell. ✓ existing
defence. Verify by integration test.

**PR-68 🔴 ❌** DB locked by external client (user opens
ghost_shell.db in DB Browser, holds write transaction):
dashboard requests time out. UI shows generic "DB error".
Fix: surface SQLite BUSY as friendly "another tool has the DB
open" message.

**PR-69 🟠 ❌** Disk full during run: log rotation fails silently,
selfcheck.json save fails, payload write fails. Run continues
but artifacts incomplete. Fix: pre-flight free-space check (≥1GB).

**PR-70 🟠 ❌** Network drop during run: Chrome's traffic stalls
while our heartbeat thread keeps refreshing the lock — looks
"alive" externally. Run never completes meaningful work.
Fix: tie heartbeat to recent network activity (PR-65).

---

# Top fixes for next-sprint priority

In order of impact-to-effort:

| # | Issue | Severity | Effort | Notes |
|---|---|---|---|---|
| 1 | PR-04 single-create missing `profile_mark_ready` | 🟡 | 2 lines | Trivial; should be done before any other Sprint 2 work |
| 2 | PR-32 delete dir orphan-kill + verify | 🔴 | ~20 lines | Mirror the launch pipeline's orphan kill |
| 3 | PR-31 / PR-66 DB-level run liveness check | 🔴 | ~50 lines | Replace dashboard-local RUNNER_POOL with `runs.heartbeat_at` query |
| 4 | PR-33/34/35/37/42 cascade-cleanup on delete | 🟠 | ~30 lines | Vault, profile_extensions, cookie_snapshots, group_members, pending_restore |
| 5 | PR-19/RC-25 disable Save until load resolves | 🟠 | ~5 lines | Frontend |
| 6 | PR-62/RC-05 chromedriver.log PID + random suffix | 🟠 | 1 line | Today's quick fix |
| 7 | PR-63/RC-07 per-extension manifest repair lock | 🟠 | ~20 lines | Today's quick fix |
| 8 | PR-21 backend validation rotating+url pair | 🟠 | ~10 lines | Server-side guard |
| 9 | PR-2 GC orphan ready_at-NULL profiles | 🟠 | ~30 lines | Dashboard startup hook |
| 10 | PR-53 GHOST_SHELL_SKIP_PAYLOAD on attempt 4 | 🔴 | ~15 lines | Closes last "Chrome won't launch" hole |

# Recommendations for Sprint 2 (stealth) and Sprint 3 (differentiation)

Before starting on JA3 / health monitor / encrypted backup / network
observability, I'd close the 🔴-severity items above (#2 PR-32,
#3 PR-31, #10 PR-53) — they're real production risks. Then proceed
to stealth.

JA3 validation specifically depends on the launch pipeline being
rock-solid: a flaky launch that retries with different network state
breaks JA3 reproducibility. Fixing PR-53 (C++ payload skip on
attempt 4) is a prerequisite for the launch pipeline to be considered
"stable enough" to layer JA3 on top.

Network observability (Sprint 3 #8) overlaps with the heartbeat
infrastructure from Sprint 2.1 — the lock heartbeat could feed
"last-network-activity" data into the same channel for free.

Encrypted backup needs the cascade-cleanup model from PR-33/34/35
to be in place — otherwise restore-on-different-machine surfaces
orphan vault rows.

---

Files touched in audit: 0 (this is a survey). Quick-fix actions
for the 3 specific items the user called out (RC-05, RC-07, RC-25)
follow this document — see commit log.
