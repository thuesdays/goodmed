# Extensions

Shared pool with per-profile assignment. Install once, opt any subset
of profiles in. Each profile keeps its own data inside its
user-data-dir.

## Install sources

The dashboard's **🧩 Extensions** page accepts three install sources:

### Chrome Web Store

Paste a CWS URL or just the 32-char ID. Optionally search by name
first via the inline search field. The pool downloads the CRX,
verifies the signature header, unpacks it, normalizes the manifest,
and stores it under `data/extensions_pool/<id>/`.

### CRX upload

Drop a `.crx` file (CRX2 or CRX3) into the upload zone. Same pipeline
as CWS minus the download step.

### Unpacked zip

Drop a `.zip` of an unpacked-extension folder layout (the structure
Chrome expects with `--load-extension=<dir>`). Same pipeline minus
CRX-signature handling.

## ID stability

Chrome derives an extension's 32-char ID from `SHA-256(public_key)`,
each nibble translated `0->a, 1->b, ..., f->p`. Without a `key`
field in `manifest.json`, Chrome falls back to a path-based hash —
moving the pool dir would change every ID and break every assignment.

To prevent that, `add_from_*` helpers always inject a `key` into
`manifest.json` on import. The injection is a minimal byte-level
patch that preserves every original byte instead of round-tripping
through `json.dump` — important because some manifests have field
ordering that affects extension behaviour, and JSON round-tripping
loses that.

The original is kept as `manifest.json.original` next to the
modified one, for forensic reference.

## Manifest normalization

Real-world extension manifests violate the spec in small ways that
Chrome tolerates only sometimes. The pool runs each on import
through:

- **default_locale** — Chrome's check is structural: if a `_locales/`
  folder exists, `default_locale` becomes required. Auto-set if
  missing.
- **manifest_version** — auto-detected from layout
  (`background.service_worker` → MV3, `background.scripts` → MV2,
  `action` vs `browser_action`, etc.) when missing.
- **required-fields fallback** — when `__MSG_*` placeholders can't be
  resolved against any locale's `messages.json`, we substitute a
  reasonable string instead of letting Chrome reject the extension.
- **match-pattern sanitization** — preserves `<all_urls>`,
  `http://*/*`, `https://*/*`, `*://x.com/*`, etc. correctly. Strips
  ones Chrome will reject outright.

If you encounter an extension that fails to install, try the upload
path with the unpacked zip — the error from the import endpoint will
be specific about which manifest field broke validation.

## Toolbar pinning

Chrome 138+ stores pinned-extension state under
`extensions.pinned_actions` in `Default/Preferences`. Older versions
(91-137) used `extensions.pinned_extensions`. Even older (88-90)
used a legacy `extensions.toolbar`.

`_ext_pre_accept_prefs` writes **all three** keys with the same
extension-ID list before launching Chrome. Chrome silently ignores
the keys it doesn't recognize for its version. Net effect: assigned
extensions show on the toolbar from the very first launch on any
Chrome 88-149.

If you manually unpin via the puzzle-piece menu, the unpin survives
only until the next launch (we re-pin every launch). For permanent
unpin, modify the per-profile assignment instead of using the puzzle
menu.

## Permission auto-accept

`_ext_pre_accept_prefs` also pre-fills the
`extensions.granted_permissions` map for every assigned extension,
so the first-launch permission dialog ("This extension wants to read
and change all data on websites you visit") never appears. Without
this, automated runs would block on the dialog forever.

## Automation: seven flow steps

Drive extensions from script flows via these new step types (palette
category "Extensions", purple `#a855f7`):

| Step | What it does |
|---|---|
| `open_extension_popup` | Opens `chrome-extension://<id>/popup.html` in a new tab. We use the popup HTML in a tab rather than triggering the toolbar icon — survives focus loss and stays scriptable. |
| `open_extension_page` | Open a custom page (`popup` / `options` / `home` / arbitrary path under the extension root). |
| `extension_wait_for` | Wait for a CSS selector inside the extension page, with timeout. |
| `extension_click` | Click an element by selector inside the extension popup/page. |
| `extension_fill` | Type into an input. Vault placeholders work: `{vault.<id>.password}` resolves at runtime. |
| `extension_eval` | Run arbitrary JS inside the extension page; return value goes into `ctx.vars[store_as]`. |
| `extension_close` | Close the popup tab and switch back to the previous one. |

### Vault placeholders

Anywhere a step accepts a string value (not just `extension_fill`),
substrings of the form `{vault.<id>.<field>}` resolve at runtime
against the unlocked vault. The vault must be unlocked before the
run starts; placeholder resolution fails noisily if it isn't.

This way, secrets never appear in saved scripts — the script just
references vault item IDs.

### Starter template

`scripts_templates/metamask_unlock.json` is a working flow that opens
the MetaMask popup, fills the password from a vault item, waits for
the wallet UI to load, and closes the popup. Use it as a starting
point for any wallet/extension automation.

## Per-profile assignment

On the **profile detail** page, the **Extensions** card shows chips
for every extension currently assigned. Click **+ Add from pool** to
open a picker; click an `×` on a chip to remove. Changes apply on
the next profile launch.

Storage: assignments live in the `profile_extensions` table (see
[`DATABASE.md`](https://github.com/thuesdays/ghost_shell_browser/blob/main/DATABASE.md)).

## Pool repair

The dashboard runs a one-shot pool repair on startup, plus a smaller
repair on each profile launch. Repair fixes:

- Missing `key` injection (was the manifest auto-modified after
  import?)
- Stale `manifest.json.original` (older format than current normalizer)
- Orphan extension dirs (in pool but no `extensions` table row)
- Orphan rows (in table but no on-disk dir)

If you ever notice "extension not found" errors after manually
mucking around in `data/extensions_pool/`, restarting the dashboard
usually heals it.
