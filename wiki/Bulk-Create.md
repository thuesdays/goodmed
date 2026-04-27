# Bulk Create

Create 1–100 profiles in a single dialog. Useful when you need a
fleet — agency-style management of multiple clients, QA environment
matrices, fingerprint-research populations.

## Open the dialog

Sidebar → **Profiles** → **⚡ Bulk create**.

## Fields

| Field | What it does |
|---|---|
| **Count** | How many profiles to create (1–100). |
| **Name pattern** | A printf-style template; `{n}` is replaced with the running index. Default: `profile_{n:02d}`. Examples: `client_acme_{n}`, `qa_pixel_{n:03d}`. |
| **Template** | Device template applied to every created profile. The template's category (desktop / mobile) drives initial fingerprint generation. Mix manually after via the Fingerprint editor if you need variety. |
| **Proxy assignment** | Pick from the proxy library. Round-robin distributes profiles across the selected proxies. "All proxies in library" picks the full set. |
| **Tags** | Tags applied to every created profile. The first non-system tag also feeds the cookie-pool category for the auto-inject feature below. |
| **Bind script** | Optional — assigns this script to every profile. Equivalent to setting `use_script_on_launch=true` per profile. |
| **Auto-inject cookies from pool** | Checkbox. When on, each profile's first launch auto-restores the best-matching pool snapshot (see [Cookie Pool](Cookie-Pool.md) for the matching algorithm). |

## Outcome states

After the API call returns, the dialog shows one of three end states:

- **Full success** — `created == count, failed == 0`. Toast `✓ Created N profile(s)`. Modal auto-closes after 900ms so the user can read the toast.
- **Partial failure** — `failed > 0` but `created > 0`. Modal stays open with a per-row error table. The action button switches to **Close**.
- **Total failure** — `created == 0, failed == count`. Same per-row error table; action button is **Close**.

The 900ms grace on auto-close is deliberate — instant close felt
broken; nothing close felt unresponsive.

## Per-profile error reasons

Common ones:

- **Name collision** — that name already exists. Adjust the pattern
  or delete the conflicting profile first.
- **Invalid template** — template removed or renamed. Pick another.
- **Proxy library empty** — when "round-robin" is selected but no
  proxies in the library. Either add proxies first or pick "no
  proxy" for these profiles.
- **Cookie-pool match failed** — auto-inject was on but no donor
  snapshots match the new profile's category/country. The profile
  is still created; just without seeded cookies. Take a manual
  donor snapshot first if seeding matters.

## Behind the scenes

The endpoint is `POST /api/profiles/bulk-create`, body:

```json
{
  "count": 25,
  "name_pattern": "client_acme_{n:02d}",
  "template_id": "desktop_chrome_win_2024",
  "proxy_strategy": "round-robin",
  "proxy_pool": ["proxy_us_residential_1", "proxy_us_residential_2"],
  "tags": ["client_acme", "production"],
  "script_id": 7,
  "use_script_on_launch": true,
  "auto_inject_cookies": true
}
```

Response:

```json
{
  "created": 24,
  "failed": 1,
  "errors": [
    {"index": 13, "name": "client_acme_14", "reason": "name collision"}
  ],
  "profile_names": ["client_acme_01", "client_acme_02", ...]
}
```

## Tips

- **Round-robin with N proxies, M profiles** — profile 1 gets proxy
  1, profile 2 gets proxy 2, ..., profile N+1 wraps to proxy 1. If
  you want one proxy per profile, set `count == len(proxy_pool)` or
  add more proxies first.
- **Name pattern with leading zeros** — use `{n:02d}` to get
  `01..09` instead of `1..9`. Sorts correctly in the Profiles
  table.
- **Don't bind a script you haven't tested** — if the script throws
  on first launch, every newly-created profile will end up with a
  failed first run. Run one profile manually first to confirm.
