# Cookie Pool

Cross-profile cookie injection. Warm a donor profile to a steady
state (logged in, history populated, consents accepted), snapshot
its cookies, then seed fresh production profiles from that snapshot
— so they don't start cold.

## Snapshot taxonomy

Snapshots come from three sources, distinguished by the `trigger`
column on `cookie_snapshots`:

- `auto_after_run` — `main.py` calls `snapshot_after_run()` at the
  end of every clean run (`exit_code=0` and no captchas). Most
  pool entries come from here.
- `manual` — user clicked **Snapshot now** on the Session page.
- `import` — uploaded JSON file from the Snapshots tab.

Each row stores `cookies_json`, `storage_json` (localStorage +
sessionStorage), `cookie_count`, `domain_count`, `bytes`, plus a
free-text `reason`.

## Restore-at-next-launch

Restoring is a two-step async dance because Chrome can only accept
cookies via CDP, which requires the browser to be running:

1. UI click **Restore** → writes `session.pending_restore.<profile>`
   into `config_kv`, with the snapshot ID as the value.
2. Next browser launch reads that key, injects the cookies via the
   CDP `Network.setCookies` call, then deletes the `config_kv` key
   so we don't restore twice.

If the restore is queued but you never launch the profile, the
queued restore stays put indefinitely. Cancel via the UI's
**Cancel pending restore** button.

## Cross-profile injection (donor → production)

The new pool API lets you inject a donor profile's cookies into a
freshly-created profile *without* opening the donor first:

- `POST /api/cookies/pool/inject` with `{profile, snapshot_id}` —
  the same as Restore-at-next-launch, but you can target any
  destination profile, not just the snapshot's source profile.
- `GET /api/cookies/pool/match?profile=…` returns recommended
  snapshots ranked by category/country match against the
  destination profile's tags.
- `GET /api/cookies/pool` lists every snapshot with metadata.

Bulk-create wires this in: when the bulk dialog has "auto-inject
cookies from pool" checked, each new profile's first launch
auto-restores the best-matching pool snapshot.

### Browserless write path

The bulk-create case can't use CDP — there's no running browser at
profile-creation time. Instead, the inject endpoint writes directly
to `<profile_user_data_dir>/Default/Cookies` (the SQLite file Chrome
reads on startup) before the first launch. CDP session-restore picks
up everything else. This is faster than launching a hidden browser
just to inject cookies.

The actual write goes through `session/cookies.py`'s
`write_cookies_to_db()` — the same function the test suite uses.

## Country / category matching

The match algorithm scores donor snapshots against the destination
profile by:

- **Tag overlap** — first non-system tag on the profile is treated
  as the cookie-pool category (e.g. `gambling`, `news`, `crypto`).
  Donors tagged with the same category score higher.
- **Country / proxy locale** — the proxy's geo (from cached
  diagnostics) compared to the donor profile's proxy at snapshot
  time.
- **Recency** — newer snapshots score higher; very old snapshots
  are likely stale.

Tweak the weights in `session/cookies.py::score_snapshot_for_profile`.

## Snapshot pruning

Snapshots aren't auto-pruned. They can pile up — every clean run
adds one. Cleanup queries:

```sql
-- Keep only the most recent N per profile
DELETE FROM cookie_snapshots
WHERE id NOT IN (
    SELECT id FROM cookie_snapshots c
    WHERE (
        SELECT COUNT(*) FROM cookie_snapshots
        WHERE profile_name = c.profile_name AND id > c.id
    ) < 10
);

-- Drop everything older than 30 days
DELETE FROM cookie_snapshots
WHERE created_at < datetime('now', '-30 days');

VACUUM;
```

The dashboard's Snapshots tab also has a per-row delete button.

## When *not* to use pool injection

- **First-party logins** — if the donor was logged into a site that
  fingerprints heavily (banks, exchanges), copying cookies into a
  different fingerprint usually triggers a re-login or a security
  challenge. The session is bound to the device, not just the
  cookies.
- **CSRF-bound sessions** — some session cookies are paired with
  per-request CSRF tokens. Cookies alone won't carry the session
  forward.
- **Mismatched proxy / geo** — if the donor was on a US proxy and
  you're injecting into an EU-proxy profile, expect challenge
  flows.

For straight session-continuity (consents, language preferences,
small UI state), pool injection is the cheapest way to skip the
cold-start tax.
