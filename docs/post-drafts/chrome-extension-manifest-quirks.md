# Five things Chrome's extension docs don't tell you about manifests

> Draft for dev.to / HN. Author: Mykola Kovhanko (@thuesdays).
> Honest engineering write-up about extension-installer pitfalls
> hit while building [Ghost Shell Anty](https://github.com/thuesdays/ghost_shell_browser)
> — a self-hosted antidetect browser. The post is about
> manifests; the project is just the context.

I built a Chrome extension installer over the last few months — the
piece that takes a `.crx` or unpacked `.zip`, validates it,
normalizes the manifest, and lays it out on disk so a custom
Chromium build can load it via `--load-extension=`. Sounds
mechanical. It wasn't. The Chrome team's
[docs](https://developer.chrome.com/docs/extensions/reference/manifest)
describe the manifest schema honestly enough, but they leave out
a handful of operational details that you only learn by trying to
install thousands of real-world extensions and watching them fail.

Here are five things I wish someone had told me before I started.

## 1. `default_locale` is a *structural* requirement, not a string requirement

The docs say `default_locale` is optional. They don't say:

**If a `_locales/` folder exists in your extension, `default_locale`
becomes mandatory.**

Chrome's check is: walk the extension dir, find `_locales/`, fail
the install if `manifest.json` doesn't declare which one is the
default. The error message is `Default locale not found` — not
"missing field" — so debugging looks like:

> "Default locale not found"
> *(stares at folder containing eight perfectly valid `_locales/<lang>/messages.json` files)*

This bites a *lot* of real-world extensions. Maintainers shipping
internationalized extensions often have the locales but not the
field — Chrome's first-party loader had quietly inferred a default
in older versions, then started failing later. My installer now
auto-injects `default_locale` based on what's in the folder. If
`_locales/en/` exists, that wins. Otherwise the first locale dir
alphabetically.

This is the kind of thing the docs probably can't easily say,
because it's emergent from the source rather than designed. But
it's what determines whether your install succeeds.

## 2. The pin-state preference key changed three times across Chrome 88-149

If you're scripting extension management — pinning particular
extensions to the toolbar pre-launch, for instance — you have to
write into Chrome's `Default/Preferences` JSON before launch. The
key for "which extensions are pinned to the toolbar":

| Chrome version | Key |
|---|---|
| 88-90 | `extensions.toolbar` |
| 91-137 | `extensions.pinned_extensions` |
| 138+ | `extensions.pinned_actions` |

Chrome silently ignores the keys it doesn't recognize for its
version. So my first attempt — write `pinned_extensions` because
that's what I'd seen in older extension-management posts — looked
like it worked (no error!), but extensions never actually showed up
on the toolbar in our Chrome 149 build.

The fix: write **all three keys** with the same ID list.
Idempotent, self-healing, survives the next rename.

The lesson generalizes: when you're working with Chrome's preference
file, **manually pin once in your target version, then diff
`Preferences` against a fresh profile**. That tells you the actual
key, not what the docs imply.

## 3. Manifests do *not* accept comments — but real-world ones contain them

Chrome's loader rejects `manifest.json` containing `//` line
comments or `/* block */` comments. JSON spec compliance.

But: the developer-facing tooling (some VSCode extensions, some
build tools) sometimes leaves comments in manifests during dev. A
non-trivial chunk of CRXs uploaded to the Chrome Web Store contain
comment artifacts that *the Web Store packager strips on upload*
but that survive in unofficial mirrors and re-packaged CRXs.

So I added a comment-stripper. A regex like:

```python
re.sub(r'/\*[\s\S]*?\*/', '', manifest_text)
```

It seemed fine. Until it wasn't.

**The bug**: this regex isn't anchored to comment boundaries. It
matches the first `/*` it finds and the next `*/`, regardless of
what's between them. Including string contents.

A real wallet extension's manifest had:

```json
{
  "matches": [
    "http://Web/*",
    "https://*/*",
    "*://wallet.example.com/*"
  ],
  ...
}
```

The regex started matching at the `/*` inside `"http://Web/*"`,
greedy-skipped to the next `*/` which was somewhere in
`https://*/*` two lines down — and **silently ate ~250 chars of
valid JSON between them**, including most of `content_scripts` and
half of `host_permissions`.

JSON parser then succeeded on the truncated text (everything still
balanced!), Chrome installed the extension, and it failed to do
anything because half its config was gone. Symptom: "extension
installs but doesn't work." No errors anywhere.

**The fix**: don't comment-strip at all. Chrome rejects manifests
with comments, so let the parser hand back the raw error. If a
user uploads a manifest with `//` lines, give them the underlying
"comments not allowed in JSON" error and let them clean it up
manually. Comment-stripping was solving a problem (some real CRXs
have comments) but creating a worse one (silent JSON corruption).

The lesson: **never run a regex pre-pass on data you'll then parse
properly.** Either hand the raw bytes to the proper parser, or
preserve the original perfectly and operate on the parsed tree.
Regex pre-passes that "just clean things up a bit" love to bite
you on weird real-world inputs.

## 4. Multiple `--disable-features=` flags don't merge — the later one wins

This isn't strictly a manifest issue, but it bit me in the same
project. Chrome accepts `--disable-features=Foo` to disable
specific feature flags. Pass it twice:

```
chrome --disable-features=Foo --disable-features=Bar
```

You'd reasonably expect both Foo and Bar to be disabled. They're
not. **Only Bar is** — the second `--disable-features` overwrites
the first.

Same for `--enable-features`, `--enable-blink-features`, and
several other flag names that take comma-separated values.

I had two layers of code, both wanting to disable specific
features. They each appended their own `--disable-features=...`.
Result: only the second one's exclusions took effect, and the
first layer's features were silently re-enabled.

**The fix**: a central join. All disable-features sources collected
into one list, comma-joined into a single flag.

```python
disabled = ["Foo", "Bar", "Baz"]
args.append(f"--disable-features={','.join(disabled)}")
```

This is documented somewhere if you read the chromium source
carefully, but it's not the obvious behaviour from the flag
syntax. Worth knowing.

## 5. Extension IDs are sticky to the public key, not the path — usually

Chrome derives an extension's 32-character ID from
`SHA-256(public_key)`, with a custom alphabet (each nibble
translated `0->a, 1->b, ..., f->p`). The public key for a CWS or
CRX-installed extension comes from the CRX file's signature
header. That's the standard, documented behaviour.

What's **not** as obvious: for unpacked extensions loaded via
`--load-extension=<dir>` and **without** a `key` field in
`manifest.json`, Chrome falls back to a *path-based hash* for the
ID. Move the directory, the ID changes. Move the parent directory,
the ID changes. Rename a parent dir, the ID changes.

Why this matters for a self-hosted extension manager: your code
now has a stable ID it stored in a database to map "this profile
uses this extension." If a user moves the install dir, every
extension gets a new ID, every assignment breaks, no errors thrown.

**The fix**: `key` injection on import. When you load an unpacked
extension, you can put a `"key": "<base64-pubkey>"` field into the
manifest, and Chrome will use *that* to derive the ID instead of
the path. The key just has to be a valid public key — you can
generate one yourself; it doesn't have to match anything real
(the extension never gets uploaded anywhere).

I auto-inject a deterministic key derived from the extension's
content on import. The ID stays stable across moves.

**Bonus footgun**: `json.load` + `json.dump` round-trip changes
manifest formatting subtly — quote style, field ordering,
whitespace. Some extensions are sensitive to manifest field
ordering (they shouldn't be, but they are). The robust fix is to
**inject the `key` field via byte-level patch**, preserving every
other byte exactly. Find the `{` opening brace, find the first
real key-value pair, inject `"key": "..."` before it without
disturbing anything else. Slightly gross code, but bulletproof.

---

## What I'd do differently

If I were starting again, I'd:

- **Test the installer against the top-100 CWS extensions on day
  one.** Most of these manifest quirks emerge from the long tail of
  weird real-world extensions, and I built mine against a small
  sample of clean ones and only discovered the rest in production.

- **Write a "raw fields preserved" diff tool** alongside the
  installer. Round-trip a manifest through your normalizer, diff
  the bytes, and assert nothing changed except the things you
  intended. Catches the regex-eating-JSON class of bug instantly.

- **Read more random extensions' source.** Half my surprises came
  from "what does a *typical* MetaMask / OKX / uBlock Origin
  manifest actually look like." The answer: weirder than you'd
  think, but consistently weird in ways that are predictable
  once you've seen 20 of them.

The full project is
[Ghost Shell Anty](https://github.com/thuesdays/ghost_shell_browser)
on GitHub if you want to look at the actual installer code
([`ghost_shell/extensions/pool.py`](https://github.com/thuesdays/ghost_shell_browser/blob/main/ghost_shell/extensions/pool.py)).
MIT licensed — feel free to lift any of it.

The wiki has a longer
["Critical Lessons"](https://github.com/thuesdays/ghost_shell_browser/wiki/Critical-Lessons)
page with five more of these — including the time my comment-
stripping regex ate part of `web_accessible_resources` in a way
that made debugging take all afternoon.

Hope this saves someone else a few afternoons.
