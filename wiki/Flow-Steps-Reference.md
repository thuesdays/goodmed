# Flow Steps Reference

The script runner (`ghost_shell.actions.runner`) executes a list of
steps the user composed in the dashboard's Scripts page. Each step is
a JSON object with a `type` plus type-specific fields.

## Browser navigation

| Step | Fields | Notes |
|---|---|---|
| `visit` | `url` | Direct navigation. `{var}` substitution. |
| `open_url` | `url` | Alias of `visit` with a clearer name for non-Google flows. |
| `back` | (none) | `browser.back()` with small delay. |
| `new_tab` | (none) | Open blank new tab. |
| `close_tab` | (none) | Close current; switch back to opener. |
| `switch_tab` | `index` | 0-based tab index. |

## Interaction

| Step | Fields | Notes |
|---|---|---|
| `click_ad` | (none) | Real DOM click on the current ad element — triggers Google's `/aclk?sa=L` tracker. Most realistic ad-click signal. Use only when monitoring **your own** ad presence. |
| `click_selector` | `selector`, `probability?`, `hover_first?`, `new_tab?` | Click any element by CSS selector. |
| `hover` | `selector`, `dwell_ms?` | Mouse-over and pause. |
| `move_random` | (none) | Curved-path random mouse move. |
| `scroll` | `direction?`, `pixels?`, `count?` | Human-like scrolling with variable speed and back-scrolls. |
| `read` | `min_sec?`, `max_sec?` | Scroll-pause-scroll mimicking a reader. |
| `type` | `selector`, `text` | Per-char delay 40-180ms. No paste. `{var}` substitution. |
| `press_key` | `key` | One key (ENTER, ESCAPE, TAB, F5, …). |
| `select_text` | `selector` | Drag-select element text. |
| `dwell` | `min_sec`, `max_sec` | Just wait. |
| `random_delay` | `kind` | `small` (1-3s), `medium` (4-8s), `long` (10-20s). |
| `scroll_to_bottom` | `chunk_px?`, `pause_ms?` | Scroll all the way down in chunks. |

## Generic web automation

Added for non-Google use cases (Twitter, FB, exchanges, scraping, QA).

| Step | Fields | Notes |
|---|---|---|
| `fill_form` | `selector`+`value`, or `fields: [{selector,value}, ...]` | Multi-field form fill, human keystroke timing. |
| `extract_text` | `selector`, `attr?`, `store_as` | Pull `.text` or an attribute into `ctx.vars[store_as]`. |
| `execute_js` | `script`, `store_as?` | Arbitrary JS in page context, run as function body — `return X` sends X into `ctx.vars`. Power tool. |
| `wait_for` | `selector`, `timeout_ms?` | Wait until selector appears. Times out gracefully. |

## Extensions (new in v0.2.0.11)

Open / interact with Chrome extensions from a flow.

| Step | Fields | Notes |
|---|---|---|
| `open_extension_popup` | `extension_id` | Opens `chrome-extension://<id>/popup.html` in a new tab — survives focus loss, fully scriptable. |
| `open_extension_page` | `extension_id`, `page` (popup / options / home / arbitrary path) | Custom page within the extension. |
| `extension_wait_for` | `selector`, `timeout_ms?` | Wait for selector inside the extension page. |
| `extension_click` | `selector` | Click within the extension page. |
| `extension_fill` | `selector`, `value` | Type into an input. `{vault.<id>.password}` placeholders resolve at runtime. |
| `extension_eval` | `script`, `store_as?` | JS in extension page context. |
| `extension_close` | (none) | Close the popup tab and switch back. |

Starter template: `scripts_templates/metamask_unlock.json`.

## Control flow

| Construct | Fields | Notes |
|---|---|---|
| `foreach_ad` | `steps: [...]` | Iterate over every captured ad on the SERP. Inside: `{ad.domain}`, `{ad.title}`, etc. resolve per iteration. |
| `if` | `cond`, `then: [...]`, `else?: [...]` | Conditional branch. See conditions list below. |
| `loop` | `count`, `steps: [...]` | Fixed-count loop. |
| `set_var` | `name`, `value` | Set a variable (supports `{...}` templates). |
| `comment` | `text` | No-op for documentation. |

### Conditions

For use in `if.cond`:

- `ads_found` — at least one ad was captured this iteration
- `ads_count_gte` — `value: N` — at least N ads
- `captcha_present` — captcha detected on page
- `var_eq` — `name`, `value` — variable equals literal
- `var_neq` — variable not equal
- `var_truthy` — variable resolves truthy (any non-empty / nonzero)
- `var_falsy` — opposite
- `selector_present` — `selector` appears in DOM
- `selector_absent` — opposite
- `url_contains` — `value` substring in current URL
- `random_lt` — `value: 0..1` — branch with given probability
- `time_between` — `from`, `to` — current local time within window
- `weekday_in` — `days: [0..6]` — current weekday in list

## Variables and substitution

Anywhere a string field appears, `{varname}` substitutes the
variable's current value. Variables come from:

- `set_var` steps (script-local)
- `extract_text` and `execute_js` (`store_as`)
- Per-iteration variables in `foreach_ad` (`{ad.domain}`,
  `{ad.title}`, `{ad.display_url}`, …)
- Vault references: `{vault.<item_id>.<field>}`
- Profile references: `{profile.name}`, `{profile.proxy.country}`

Substitution is shallow — one pass, no nested `{...}` expansion.

## Logging

Every step writes one row to `action_events` with:

- `run_id`, `profile_name`, `timestamp`
- `step_index`, `step_type`, `params_json`
- `outcome` (ok / failed / skipped)
- `error` (text, on failure)
- `duration_ms`

The Logs page renders a live tail; the Runs page links per-run
event lists with deeper inspection.

## Building a flow

The Scripts page has a visual editor with palette categories
(Navigation / Interaction / Web automation / Extensions / Control /
Variables / Comment). Drag steps into the canvas, configure fields
in the right-hand inspector. Save with a name; one is flagged
default. Profiles can override the default per-profile.

`scripts_templates/` ships a few starting points — check them out
before building from scratch.
