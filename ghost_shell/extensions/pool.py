"""
ghost_shell.extensions.pool — File-system + parsing layer for the
Chrome extension pool.

The pool is a directory containing one subfolder per extension, named
by the extension's canonical Chrome ID. Each subfolder is a fully-
unpacked extension (the same layout Chrome expects with
`--load-extension=<dir>`).

    data/extensions_pool/
        cgcoblpapocaiplgmhlhgaipmddglngm/   <- OKX wallet
            manifest.json
            popup.html
            background.js
            ...
        nkbihfbeogaeaoehlefnkodbefgpgknn/   <- MetaMask
            ...

Why one shared pool instead of per-profile copies:
  * Disk space: a serious extension is 5-30 MB; 50 profiles = 250 MB
    of duplicated bytes vs ~10 MB shared.
  * Updates: re-download once, every profile gets the new version on
    next launch.
  * Permissions / consistency: all profiles see the exact same code
    surface, no risk of drift after a manual edit.

Per-profile EXTENSION DATA (cookies, IndexedDB, login state, settings)
lives inside the user-data-dir at:
    <profile_user_data_dir>/Default/Local Extension Settings/<id>/
    <profile_user_data_dir>/Default/IndexedDB/chrome-extension_<id>_0.indexeddb.leveldb/
    <profile_user_data_dir>/Default/Storage/ext/<id>/
This is fully isolated per-profile and survives across launches without
us doing anything -- Chrome handles persistence as long as we keep
pointing at the same source dir on subsequent launches.

ID derivation (32-char lowercase a-p):
  Chrome derives an extension's ID from its public key:
      id = first 16 bytes of SHA-256(pubkey_DER), each nibble
           translated 0->a, 1->b, ..., 9->j, a->k, ..., f->p
  For CWS / CRX-loaded extensions: pubkey comes from the CRX file
  itself (the .crx binary header).
  For unpacked extensions WITHOUT a `key` field in manifest.json:
  Chrome uses path-based hashing instead, which means the same code
  would get a DIFFERENT id if you moved the folder. To avoid breakage
  when our pool dir layout changes, we ALWAYS auto-inject a `key`
  field into manifest.json on import — that pins the id to the
  manifest content rather than the path.
"""

from __future__ import annotations

__author__ = "Mykola Kovhanko"
__email__  = "thuesdays@gmail.com"

import base64
import hashlib
import io
import json
import logging
import os
import shutil
import struct
import urllib.parse
import zipfile
from typing import Optional

from ghost_shell.core.platform_paths import PROJECT_ROOT


# ────────────────────────────────────────────────────────────────
# Pool directory
# ────────────────────────────────────────────────────────────────

POOL_DIR = os.path.join(PROJECT_ROOT, "data", "extensions_pool")


# RC-07: per-extension repair lock. Two profiles launching at the same
# time both call the manifest repair pass on the same shared pool dir.
# Without serialisation, both can write manifest.json simultaneously —
# last writer wins, intermediate state could be a partially-written
# JSON file that the OTHER reader sees as truncated. The lock is keyed
# by pool path (one lock per extension), so different extensions can
# still repair concurrently. The dict is module-level and lock-protected
# so dict mutation itself is safe.
import threading as _threading
_REPAIR_LOCKS_REGISTRY: dict = {}
_REPAIR_LOCKS_GUARD = _threading.Lock()


def _get_repair_lock(pool_path: str):
    """Return the (singleton) lock for the given pool dir. Creates one
    on first use. Pool path is normalized for safe dict lookup across
    forward/back-slash variants."""
    key = os.path.normpath(pool_path).lower()
    with _REPAIR_LOCKS_GUARD:
        lk = _REPAIR_LOCKS_REGISTRY.get(key)
        if lk is None:
            lk = _threading.Lock()
            _REPAIR_LOCKS_REGISTRY[key] = lk
        return lk



def _ensure_pool_dir() -> str:
    os.makedirs(POOL_DIR, exist_ok=True)
    return POOL_DIR


def pool_path(ext_id: str) -> str:
    """Absolute path to an extension's unpacked folder in the pool."""
    return os.path.join(POOL_DIR, ext_id)


def remove_from_pool(ext_id: str) -> bool:
    """Delete the pool folder for an extension. Caller is responsible
    for removing DB rows separately (or the FK cascades it)."""
    p = pool_path(ext_id)
    if not os.path.exists(p):
        return False
    try:
        shutil.rmtree(p)
        return True
    except Exception as e:
        logging.warning(f"[ext-pool] couldn't remove {p}: {e}")
        return False


# ────────────────────────────────────────────────────────────────
# ID derivation
# ────────────────────────────────────────────────────────────────

_HEX_TO_CHROME = str.maketrans("0123456789abcdef", "abcdefghijklmnop")


def extension_id_from_pubkey(pubkey_der: bytes) -> str:
    """Chrome extension ID = first 16 bytes of SHA-256(pubkey_der),
    rendered as 32 lowercase chars in the a-p alphabet (each nibble
    of the hex digest shifted up by 0x61)."""
    digest = hashlib.sha256(pubkey_der).hexdigest()[:32]
    return digest.translate(_HEX_TO_CHROME)


def _generate_dummy_pubkey(seed: str) -> bytes:
    """For unpacked extensions that have no `key` we deterministically
    generate one from a seed (the manifest contents). Result is a
    fake DER-encoded RSA public key — Chrome doesn't validate the
    cryptographic structure when the extension is loaded via
    --load-extension, it just hashes whatever bytes we put in `key`
    to derive the ID. So a deterministic SHA-512 of the manifest is
    perfectly stable across pool dir moves."""
    # Use SHA-512 → 64 bytes → base64 → ~88 chars, plenty for a
    # plausible-looking key. We prefix it with a few RSA-like bytes
    # so Chrome doesn't get angry on the format check (the prefix
    # mimics a 1024-bit RSA SubjectPublicKeyInfo header — Chrome
    # tolerates anything that decodes as base64).
    body = hashlib.sha512(("ghost-shell-pinned:" + seed).encode("utf-8")).digest()
    # The actual bytes that get hashed for the ID. Pad to 162 bytes
    # (typical RSA-1024 SPKI length) so the produced key looks
    # right when inspected in chrome://extensions.
    padded = body + (b"\x00" * (162 - len(body)))
    return padded


# ────────────────────────────────────────────────────────────────
# Manifest helpers
# ────────────────────────────────────────────────────────────────

def parse_manifest(manifest_path: str) -> dict:
    """Read manifest.json. Returns {} on failure.

    HARD-LEARNED LESSON (OKX wallet 2026-04): the previous version of
    this function ran regex preprocessors over the raw text to strip
    `/* ... */` and `// ...` comments before json.loads. That worked
    for hand-written manifests with comments, but it ATE valid content
    in real CRX manifests because:

      - "host_permissions": ["http://*/*", "https://*/*"]
        contains both `/*` and `*/` substrings — the block-comment
        regex matched from one to the other and silently deleted
        everything in between (including content_scripts,
        host_permissions, web_accessible_resources entries).

      - paths like "static/fonts/HarmonyOS_Sans_Web/*" trigger the
        same: `/*` after `Web` paired with the `*/` in the next
        `https://*/*` matches array a few keys later → ~250 chars
        of valid JSON silently dropped.

    Real Chrome manifests are pure JSON. Chrome itself does NOT accept
    JSON-with-comments in manifest.json. So stripping comments is both
    unnecessary and dangerous. Only handle:
      - UTF-8 BOM (utf-8-sig encoding)
      - Trailing commas (rare but valid in some hand-edited manifests)
    """
    if not os.path.exists(manifest_path):
        return {}
    try:
        with open(manifest_path, "r", encoding="utf-8-sig") as f:
            text = f.read()
        # Conservative: only strip trailing commas before } or ].
        # This regex won't match inside a properly-escaped string
        # because real JSON strings can't contain `,]` or `,}` — those
        # tokens only appear at structural positions.
        import re as _re
        text = _re.sub(r",(\s*[}\]])", r"\1", text)
        return json.loads(text)
    except Exception as e:
        logging.warning(f"[ext-pool] parse_manifest failed for {manifest_path}: {e}")
        return {}


def _ensure_stable_key(pool_dir: str, ext_id_hint: str = None) -> str:
    """Read manifest.json from a freshly-extracted pool dir, normalize
    it (apply all manifest fixes), ensure it has a `key` field, write
    it back, and return the resulting extension ID.

    Manifest fixes applied to EVERY install:
      - _ensure_default_locale: Chrome rejects extensions where _locales/
        exists but default_locale is unset
      - _ensure_required_fields: name + version must be non-empty
        strings; resolves __MSG_xxx__ placeholders or strips them
      - _sanitize_match_patterns: strips invalid URL schemes from
        content_scripts.matches and host_permissions

    These run BEFORE the key decision so CWS extensions (which always
    have a key) get fixed too — without this, the fixes were silently
    bypassed for the most common install path.
    """
    manifest_path = os.path.join(pool_dir, "manifest.json")
    manifest = parse_manifest(manifest_path)
    if not manifest:
        raise ValueError(f"no manifest.json in {pool_dir}")

    # Apply all manifest fixes FIRST, regardless of which key path we
    # take. Order matters: required-fields fix needs to run AFTER
    # default_locale is set so it can resolve __MSG_ placeholders
    # against the right messages.json.
    _ensure_default_locale(pool_dir, manifest)
    _ensure_required_fields(pool_dir, manifest)
    _sanitize_match_patterns(manifest)

    existing_key = manifest.get("key")
    if existing_key:
        # base64 → DER → ID. We still want to write the patched
        # manifest back since our fixes may have changed it.
        try:
            der = base64.b64decode(existing_key)
            ext_id = extension_id_from_pubkey(der)
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
            return ext_id
        except Exception:
            # Corrupt key — fall through and overwrite
            pass

    # Synthesise a stable key from manifest content (excluding any
    # existing key / version) so re-uploads of the SAME extension
    # produce the SAME ID even if version bumped.
    seed_dict = {k: v for k, v in manifest.items()
                 if k not in ("key", "version", "version_name")}
    seed = json.dumps(seed_dict, sort_keys=True, ensure_ascii=False)
    pubkey = _generate_dummy_pubkey(seed)
    manifest["key"] = base64.b64encode(pubkey).decode("ascii")

    # Write the patched manifest back
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    return extension_id_from_pubkey(pubkey)


def _ensure_required_fields(pool_dir: str, manifest: dict) -> None:
    """Chrome requires `manifest_version` (integer 2 or 3), `name`
    (non-empty string), and `version` (non-empty string). Some
    real-world extensions in the wild are missing one or more of
    these by the time we go to load them — usually because:
      - The CRX was repackaged by a third-party tool that stripped
        the field
      - The manifest uses `__MSG_xxx__` for `name` and the message
        key is missing from messages.json (Chrome's i18n expansion
        produces an empty string)
      - `manifest_version` was omitted by an older spec (very rare)

    We patch each in place. Idempotent: a clean manifest gets no
    modifications."""

    # 1. manifest_version — must be an integer 2 or 3.
    mv = manifest.get("manifest_version")
    if isinstance(mv, int) and mv in (2, 3):
        pass  # all good
    elif isinstance(mv, str) and mv.strip().isdigit():
        coerced = int(mv.strip())
        if coerced in (2, 3):
            manifest["manifest_version"] = coerced
            logging.info(
                f"[ext-pool] manifest_version was string {mv!r} → coerced to int"
            )
        else:
            manifest["manifest_version"] = _detect_manifest_version(manifest)
            logging.info(
                f"[ext-pool] manifest_version was {mv!r} (out of range) → "
                f"detected {manifest['manifest_version']}"
            )
    else:
        detected = _detect_manifest_version(manifest)
        manifest["manifest_version"] = detected
        logging.info(
            f"[ext-pool] manifest_version was missing → detected {detected} "
            f"from schema"
        )

    # 2. name and version — must be non-empty strings, resolve __MSG_
    fallbacks = {
        "name":    f"Extension {os.path.basename(pool_dir)[:8]}",
        "version": "1.0.0",
    }
    for field, default in fallbacks.items():
        v = manifest.get(field)
        if isinstance(v, str) and v.startswith("__MSG_") and v.endswith("__"):
            key = v[len("__MSG_"):-len("__")]
            resolved = _resolve_locale_message_safe(pool_dir, manifest, key)
            if not resolved:
                manifest[field] = key.replace("_", " ").title() if key else default
                logging.info(
                    f"[ext-pool] {field}: __MSG_{key}__ not found in "
                    f"any locale → set to {manifest[field]!r}"
                )
        elif not isinstance(v, str) or not v.strip():
            manifest[field] = default
            logging.info(
                f"[ext-pool] {field} was missing/empty → set to {default!r}"
            )


def _detect_manifest_version(manifest: dict) -> int:
    """Guess MV2 vs MV3 from schema clues. Used when manifest_version
    is missing or invalid. Defaults to 3 — modern is the safer bet
    since Chrome 138+ has dropped MV2 support entirely.

    MV3 indicators (any one is enough):
      - background.service_worker
      - top-level `action` (replaces browser_action/page_action)
      - host_permissions as a separate top-level key
      - manifest contains `cross_origin_embedder_policy`

    MV2 indicators:
      - background.scripts / background.page / background.persistent
      - browser_action / page_action at top level
    """
    bg = manifest.get("background") or {}
    if isinstance(bg, dict) and bg.get("service_worker"):
        return 3
    if "action" in manifest and "browser_action" not in manifest:
        return 3
    if "host_permissions" in manifest:
        return 3
    if isinstance(bg, dict) and (bg.get("scripts") or bg.get("page")
                                 or "persistent" in bg):
        return 2
    if "browser_action" in manifest or "page_action" in manifest:
        return 2
    return 3  # default for unknown


def _resolve_locale_message_safe(pool_dir: str, manifest: dict,
                                 key: str) -> "str | None":
    """Look up a single message key. Tries default_locale first, then
    en/en_US/en_GB, then any locale. Returns None if not found anywhere."""
    if not key:
        return None
    locales_dir = os.path.join(pool_dir, "_locales")
    if not os.path.isdir(locales_dir):
        return None
    tried = []
    candidates = []
    dl = manifest.get("default_locale")
    if dl: candidates.append(dl)
    candidates.extend(["en", "en_US", "en_GB"])
    try:
        candidates.extend(sorted(os.listdir(locales_dir)))
    except Exception:
        pass
    seen = set()
    for loc in candidates:
        if loc in seen:
            continue
        seen.add(loc)
        path = os.path.join(locales_dir, loc, "messages.json")
        if not os.path.isfile(path):
            continue
        tried.append(loc)
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                msgs = json.load(f)
        except Exception:
            continue
        # Chrome's i18n is case-insensitive on the key
        for k_variant in (key, key.lower(), key.upper()):
            entry = msgs.get(k_variant)
            if isinstance(entry, dict):
                msg = entry.get("message")
                if msg: return msg
            elif isinstance(entry, str) and entry:
                return entry
    return None


def _ensure_default_locale(pool_dir: str, manifest: dict) -> None:
    """Make sure the manifest is consistent with respect to
    localization. Chrome's actual rule (extensions/common/manifest_handlers/
    default_locale_handler.cc) is STRUCTURAL — if the _locales/ folder
    exists in the extension, default_locale MUST be set. Whether or
    not the manifest uses __MSG_ placeholders is irrelevant; Chrome
    will refuse to load with "Localization used, but default_locale
    wasn't specified" the moment it sees _locales/.

    Two outcomes:
      a) _locales/ exists → set default_locale to the best available
         locale (en > en_US > en_GB > first alphabetical that has a
         valid messages.json).
      b) _locales/ doesn't exist but manifest uses __MSG_ placeholders →
         strip the placeholders to plain strings so the extension at
         least loads. (This case is rare — usually means a malformed
         CRX repackager dropped the locales folder.)

    Mutates `manifest` in place. Idempotent."""
    locales_dir = os.path.join(pool_dir, "_locales")
    has_locales_folder = os.path.isdir(locales_dir)

    # Path A: _locales/ exists — ensure default_locale is set + valid
    if has_locales_folder:
        available = []
        try:
            for entry in os.listdir(locales_dir):
                full = os.path.join(locales_dir, entry, "messages.json")
                if os.path.isfile(full):
                    available.append(entry)
        except Exception:
            pass
        current = manifest.get("default_locale")
        # If default_locale is already set AND points at a real folder,
        # we're done.
        if current and current in available:
            return
        if not available:
            # _locales/ exists but is empty — Chrome still rejects.
            # Strip the entire folder by removing the manifest reference;
            # we can't actually delete the folder from here, but Chrome
            # primarily checks default_locale presence so leaving it
            # unset is fine.
            return
        # Pick a locale: en > en_US > en_GB > first alphabetical
        pick = None
        for pref in ("en", "en_US", "en_GB"):
            if pref in available:
                pick = pref
                break
        if pick is None:
            pick = sorted(available)[0]
        old = manifest.get("default_locale")
        manifest["default_locale"] = pick
        logging.info(
            f"[ext-pool] {'fixed' if old else 'auto-filled'} "
            f"default_locale={pick!r} in {pool_dir} "
            f"(was {old!r}; available: {available})"
        )
        return

    # Path B: no _locales folder — strip __MSG_ placeholders if any so
    # the extension at least loads (Chrome would otherwise expand them
    # to empty strings).
    fields_to_check = ("name", "description", "short_name")
    stripped_any = False
    for k in fields_to_check:
        v = manifest.get(k)
        if isinstance(v, str) and v.startswith("__MSG_"):
            manifest[k] = v.replace("__MSG_", "").replace("__", "")
            stripped_any = True
    # Also clear default_locale if it points at a non-existent folder
    # — Chrome treats that the same as "_locales used but missing".
    if manifest.get("default_locale"):
        manifest.pop("default_locale", None)
        stripped_any = True
    if stripped_any:
        logging.info(
            f"[ext-pool] no _locales folder in {pool_dir} — "
            f"stripped __MSG_ placeholders + dangling default_locale"
        )


# Schemes Chrome will accept in match patterns. Anything else gets the
# whole extension rejected at load time with "Wrong scheme type".
_VALID_MATCH_SCHEMES = ("http", "https", "file", "ftp", "urn", "ws", "wss")
_VALID_MATCH_PREFIXES = tuple(s + "://" for s in _VALID_MATCH_SCHEMES) + (
    "*://",          # http + https wildcard
    "<all_urls>",    # special — matches all schemes Chrome supports
)


def _is_valid_match_pattern(pat) -> bool:
    if not isinstance(pat, str):
        return False
    pat = pat.strip()
    if not pat:
        return False
    return pat.startswith(_VALID_MATCH_PREFIXES)


def _sanitize_match_patterns(manifest: dict) -> None:
    """Remove invalid match patterns from content_scripts and from the
    top-level host_permissions / permissions arrays. Mutates `manifest`
    in place. Logs each drop so the user can see what happened.

    Chrome only accepts a small set of URL schemes in match patterns;
    anything else (chrome-extension://, moz-extension://, custom
    protocols) causes the entire extension to fail to load. This
    silently drops the bad ones rather than failing the whole load."""
    # content_scripts: each entry has matches + optional exclude_matches
    scripts = manifest.get("content_scripts")
    if isinstance(scripts, list):
        new_scripts = []
        for i, cs in enumerate(scripts):
            if not isinstance(cs, dict):
                continue
            for key in ("matches", "exclude_matches"):
                arr = cs.get(key)
                if not isinstance(arr, list):
                    continue
                kept = [p for p in arr if _is_valid_match_pattern(p)]
                dropped = [p for p in arr if p not in kept]
                if dropped:
                    logging.info(
                        f"[ext-pool] content_scripts[{i}].{key}: "
                        f"dropped invalid pattern(s): {dropped}"
                    )
                cs[key] = kept
            # Drop the entry entirely if it lost all its matches
            # (Chrome requires at least one valid match per script).
            if cs.get("matches"):
                new_scripts.append(cs)
            else:
                logging.info(
                    f"[ext-pool] content_scripts[{i}] dropped — no valid "
                    f"matches remained"
                )
        manifest["content_scripts"] = new_scripts

    # host_permissions (Manifest v3): plain list of patterns
    hp = manifest.get("host_permissions")
    if isinstance(hp, list):
        kept = [p for p in hp if _is_valid_match_pattern(p)]
        dropped = [p for p in hp if p not in kept]
        if dropped:
            logging.info(f"[ext-pool] host_permissions dropped: {dropped}")
        manifest["host_permissions"] = kept

    # permissions (Manifest v2 mixed schema-perm + host pattern):
    # only filter entries that look like URL patterns (have ://).
    perms = manifest.get("permissions")
    if isinstance(perms, list):
        kept = []
        dropped = []
        for p in perms:
            if not isinstance(p, str):
                kept.append(p); continue
            if "://" not in p and not p.startswith("<all_urls>"):
                # Pure permission keyword like "storage", "tabs" — keep as-is
                kept.append(p)
            elif _is_valid_match_pattern(p):
                kept.append(p)
            else:
                dropped.append(p)
        if dropped:
            logging.info(f"[ext-pool] permissions URL patterns dropped: {dropped}")
        manifest["permissions"] = kept

    # web_accessible_resources (MV3) entries also contain matches arrays
    war = manifest.get("web_accessible_resources")
    if isinstance(war, list):
        for i, entry in enumerate(war):
            if not isinstance(entry, dict):
                continue
            for key in ("matches", "use_dynamic_url"):
                arr = entry.get(key)
                if isinstance(arr, list):
                    kept = [p for p in arr if _is_valid_match_pattern(p)] if key == "matches" else arr
                    if key == "matches":
                        dropped = [p for p in arr if p not in kept]
                        if dropped:
                            logging.info(
                                f"[ext-pool] web_accessible_resources[{i}].matches "
                                f"dropped: {dropped}"
                            )
                        entry[key] = kept


def _icon_b64_from_pool(pool_dir: str, manifest: dict) -> Optional[str]:
    """Pull the largest available icon and return as base64 data-URI
    string ready for <img src=...>. Used by the Extensions page card."""
    icons = manifest.get("icons") or {}
    if not icons:
        # action.default_icon (Manifest v3) sometimes has it instead
        action = manifest.get("action") or manifest.get("browser_action") or {}
        ic = action.get("default_icon") or {}
        if isinstance(ic, str):
            icons = {"any": ic}
        else:
            icons = ic
    if not icons:
        return None
    # Pick the largest size
    best_path = None
    best_size = 0
    for size, rel in icons.items():
        try:
            sz = int(size) if size != "any" else 999
        except ValueError:
            sz = 64
        full = os.path.join(pool_dir, rel)
        if os.path.exists(full) and sz > best_size:
            best_path = full
            best_size = sz
    if not best_path:
        return None
    try:
        with open(best_path, "rb") as f:
            data = f.read()
        ext = os.path.splitext(best_path)[1].lower().lstrip(".")
        mime = "image/png" if ext == "png" else f"image/{ext}"
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    except Exception:
        return None


def _summarise_permissions(manifest: dict, max_items: int = 8) -> str:
    """Pretty-print top permissions for the UI card."""
    perms = list(manifest.get("permissions") or [])
    host_perms = list(manifest.get("host_permissions") or [])
    all_perms = perms + host_perms
    if len(all_perms) > max_items:
        return ", ".join(all_perms[:max_items]) + f" (+{len(all_perms) - max_items} more)"
    return ", ".join(all_perms) if all_perms else ""


# ────────────────────────────────────────────────────────────────
# CRX format parsing
# ────────────────────────────────────────────────────────────────
# CRX3 layout (the modern format Chrome 64+ uses):
#   bytes 0..3   "Cr24" magic
#   bytes 4..7   version uint32 (3 for CRX3)
#   bytes 8..11  header_size uint32 (length of CrxFileHeader protobuf)
#   bytes 12..   CrxFileHeader protobuf
#   bytes ...    ZIP archive
#
# We DON'T need to fully parse the protobuf. We just want:
#   1. Skip the header to get to the ZIP body
#   2. Extract the public key from inside the protobuf
#
# The protobuf field carrying the SHA-256 hash of the pubkey
# (sha256_with_publickey -> pub_key) is field tag 2 with wire type 2
# (length-delimited). We do a tiny manual scan rather than pulling
# in the full `protobuf` package.

CRX3_MAGIC = b"Cr24"
CRX2_MAGIC_OLD_BUT_STILL_OK = b"Cr24"  # Same magic, version differs


def _scan_crx_for_pubkey(header_bytes: bytes) -> Optional[bytes]:
    """Best-effort scan of the CRX3 protobuf header for the embedded
    DER public key. Returns the raw key bytes, or None if not found.

    The protobuf layout we want:
        message AsymmetricKeyProof {
            optional bytes public_key = 1;
            optional bytes signature  = 2;
        }
        message CrxFileHeader {
            repeated AsymmetricKeyProof sha256_with_rsa = 2;
            ...
        }

    We look for a length-delimited field starting with `0x12 <varint>`
    (field 2 wire 2 = AsymmetricKeyProof entry), then inside that
    look for `0x0a <varint>` (field 1 wire 2 = public_key). The first
    public_key encountered is the right one.
    """
    i = 0
    n = len(header_bytes)
    while i < n - 4:
        if header_bytes[i] == 0x12:  # field 2, wire 2 (length-delimited)
            # Read varint length
            length, ln = _read_varint(header_bytes, i + 1)
            if length is None or length <= 0 or length > n:
                i += 1; continue
            kp_start = i + 1 + ln
            kp_end = kp_start + length
            if kp_end > n:
                i += 1; continue
            # Inside this proof, look for field 1 (public_key)
            j = kp_start
            while j < kp_end - 1:
                if header_bytes[j] == 0x0a:  # field 1, wire 2
                    plen, pln = _read_varint(header_bytes, j + 1)
                    if plen and 32 <= plen <= 4096:
                        pk_start = j + 1 + pln
                        return bytes(header_bytes[pk_start:pk_start + plen])
                j += 1
            i = kp_end
        else:
            i += 1
    return None


def _read_varint(buf: bytes, offset: int) -> tuple[Optional[int], int]:
    """Decode a protobuf varint starting at offset. Returns (value, bytes_consumed)
    or (None, 0) on overflow."""
    val = 0
    shift = 0
    consumed = 0
    while offset + consumed < len(buf):
        b = buf[offset + consumed]
        consumed += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, consumed
        shift += 7
        if shift > 70:
            return None, 0
    return None, 0


def validate_crx_integrity(crx_bytes: bytes) -> tuple[bool, str]:
    """Polish #2 — lightweight CRX integrity validation.

    Catches the realistic tampering vectors WITHOUT pulling RSA crypto
    as a dependency. Returns (ok, reason). On ok=False, reason is a
    human-readable explanation; callers should refuse the import.

    Checks:
      1. Minimum length (not a zero-byte / placeholder file)
      2. CRX3 magic bytes (or accept plain ZIP for unpacked imports)
      3. Header length sanity (no integer-overflow attacks)
      4. ZIP central directory parses (zipfile.BadZipFile on tamper)
      5. ZIP entry count in a sane range (1 ≤ N ≤ 10000)
      6. No zip-slip: every entry name must be relative + not contain
         ".." segments. Chrome's CRX unpacker rejects these too, but
         our temp dir extraction would happily follow them.
      7. manifest.json present in the archive

    What this DOESN'T verify (would require RSA via `cryptography`):
      - That the CRX3 RSA signature actually matches the SignedData
        block. A rebundled CRX with mismatched pubkey would still
        pass this check. Acceptable trade since Chrome itself will
        refuse the broken CRX at load time, and we'd notice.
    """
    if len(crx_bytes) < 64:
        return False, "file too short to be a valid CRX"

    is_crx = crx_bytes[:4] == CRX3_MAGIC
    if is_crx:
        if len(crx_bytes) < 16:
            return False, "CRX header truncated"
        version = struct.unpack("<I", crx_bytes[4:8])[0]
        if version == 3:
            header_size = struct.unpack("<I", crx_bytes[8:12])[0]
            # Sanity: header should never exceed 1 MB. Real CRX3 headers
            # are 1-4 KB; >1 MB is either corruption or attack.
            if header_size > 1_048_576:
                return False, f"CRX3 header_size implausibly large: {header_size}"
            if 12 + header_size > len(crx_bytes):
                return False, "CRX3 header extends past EOF"
        elif version == 2:
            if len(crx_bytes) < 16:
                return False, "CRX2 header truncated"
            pubkey_len = struct.unpack("<I", crx_bytes[8:12])[0]
            sig_len    = struct.unpack("<I", crx_bytes[12:16])[0]
            if pubkey_len > 65536 or sig_len > 65536:
                return False, "CRX2 pubkey/sig length implausible"
            if 16 + pubkey_len + sig_len > len(crx_bytes):
                return False, "CRX2 header extends past EOF"
        else:
            return False, f"unsupported CRX version {version}"

    # Extract the ZIP portion (regardless of whether it's a CRX or
    # plain ZIP). _crx_to_zip handles both — but we want to call it
    # with the same logic, so just re-do the strip lightweight here:
    if is_crx:
        try:
            zip_part, _ = _crx_to_zip(crx_bytes)
        except Exception as e:
            return False, f"CRX header parse failed: {e}"
    else:
        zip_part = crx_bytes

    try:
        with zipfile.ZipFile(io.BytesIO(zip_part)) as zf:
            names = zf.namelist()
            if not names:
                return False, "ZIP is empty"
            if len(names) > 10000:
                return False, f"ZIP entry count implausible: {len(names)}"
            for n in names:
                if n.startswith("/") or n.startswith("\\"):
                    return False, f"absolute path in ZIP: {n!r}"
                # Reject any segment that's exactly ".." — the safest
                # rule. Don't try to canonicalise: forward and back
                # slashes both count as separators on Windows.
                segs = n.replace("\\", "/").split("/")
                if any(s == ".." for s in segs):
                    return False, f"zip-slip path in ZIP: {n!r}"
            if "manifest.json" not in names:
                return False, "no manifest.json in ZIP"
    except zipfile.BadZipFile as e:
        return False, f"corrupt ZIP body: {e}"
    except Exception as e:
        return False, f"ZIP probe failed: {e}"

    return True, ""


def _crx_to_zip(crx_bytes: bytes) -> tuple[bytes, Optional[bytes]]:
    """Strip CRX header, return (zip_bytes, embedded_pubkey_or_None).

    CRX2 (legacy) header is fixed length:
        magic(4) version(4) pubkey_len(4) sig_len(4) pubkey sig
    CRX3 header is variable length, prefixed by header_size uint32.
    We support both because some old CRX files still circulate.
    """
    if len(crx_bytes) < 16 or crx_bytes[:4] != CRX3_MAGIC:
        # Could be a plain ZIP — return as-is
        return crx_bytes, None

    version = struct.unpack("<I", crx_bytes[4:8])[0]
    if version == 3:
        header_size = struct.unpack("<I", crx_bytes[8:12])[0]
        body_start = 12 + header_size
        if body_start > len(crx_bytes):
            raise ValueError("CRX3 header_size out of range")
        header_bytes = crx_bytes[12:body_start]
        zip_bytes = crx_bytes[body_start:]
        pubkey = _scan_crx_for_pubkey(header_bytes)
        return zip_bytes, pubkey

    if version == 2:
        # CRX2: classic format, fixed header
        pubkey_len = struct.unpack("<I", crx_bytes[8:12])[0]
        sig_len    = struct.unpack("<I", crx_bytes[12:16])[0]
        pubkey_start = 16
        pubkey_end   = pubkey_start + pubkey_len
        sig_end      = pubkey_end + sig_len
        zip_bytes = crx_bytes[sig_end:]
        pubkey = crx_bytes[pubkey_start:pubkey_end]
        return zip_bytes, pubkey

    raise ValueError(f"unsupported CRX version: {version}")


# ────────────────────────────────────────────────────────────────
# Public API: add to pool
# ────────────────────────────────────────────────────────────────

def add_from_crx(crx_bytes: bytes,
                 source_url: str = None) -> tuple[str, str, dict]:
    """Unpack a CRX file into the pool. Returns (extension_id,
    pool_path, manifest_dict). On collision (re-import of same id)
    the existing pool dir is overwritten in place — cookies and
    storage in profile dirs are unaffected.

    CRITICAL: Real wallet manifests (MetaMask, OKX) are dense JSON
    with ~25-30 top-level keys including content_security_policy,
    web_accessible_resources, optional_permissions, externally_connectable,
    etc. Some of those use formats that python's json module accepts but
    re-serializes differently (e.g. integer keys in icons get coerced to
    strings, key order changes). To avoid silently losing fields when we
    rewrite manifest.json with json.dump, we:

      1. Save the ORIGINAL bytes as manifest.json.original for debugging
      2. Inject `key` via minimal text patching (not full re-serialize)
         when we have a pubkey — preserves every original byte except
         the inserted key field
      3. Apply normalization passes (default_locale, required_fields,
         sanitize_match_patterns) AFTER injection — these run their
         own merge logic
    """
    _ensure_pool_dir()

    # Polish #2 — CRX integrity gate. Reject obviously broken /
    # tampered / zip-slip-vulnerable archives BEFORE we extract them
    # into a temp dir. This catches: empty files, magic-byte-mangled
    # CRX, zip-slip, missing manifest. Full RSA signature verify is
    # left to Chrome's own loader.
    ok, reason = validate_crx_integrity(crx_bytes)
    if not ok:
        raise ValueError(f"CRX integrity check failed: {reason}")

    zip_bytes, pubkey = _crx_to_zip(crx_bytes)

    candidate_id = extension_id_from_pubkey(pubkey) if pubkey else None

    import tempfile
    tmp = tempfile.mkdtemp(prefix="gs_ext_unpack_")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(tmp)

        manifest_path = os.path.join(tmp, "manifest.json")
        if not os.path.exists(manifest_path):
            raise ValueError("CRX has no manifest.json")

        # 1. Save ORIGINAL bytes as backup for debugging. Includes BOM
        #    if present, exact whitespace, comments, etc. We keep this
        #    forever — easy to diff against the patched manifest.
        with open(manifest_path, "rb") as f:
            original_bytes = f.read()
        with open(manifest_path + ".original", "wb") as f:
            f.write(original_bytes)

        # 2. Inject `key` field via MINIMAL text patching. We don't do
        #    parse + dump because parse_manifest's regex preprocessors
        #    + json.dump round-trip can lose edge-case fields (e.g.
        #    fields whose values contain unescaped control chars).
        if pubkey and candidate_id:
            key_b64 = base64.b64encode(pubkey).decode("ascii")
            patched = _inject_manifest_key(original_bytes, key_b64)
            with open(manifest_path, "wb") as f:
                f.write(patched)
            ext_id = candidate_id
        else:
            ext_id = _ensure_stable_key(tmp)

        # 3. Run normalization passes (default_locale, required fields,
        #    sanitize). These do parse + dump, but they LOG every drop
        #    so the user can see what's happening. The .original backup
        #    is preserved so any field loss is recoverable.
        try:
            manifest = parse_manifest(manifest_path)
            if manifest:
                before_keys = sorted(manifest.keys())
                _ensure_default_locale(tmp, manifest)
                _ensure_required_fields(tmp, manifest)
                _sanitize_match_patterns(manifest)
                after_keys = sorted(manifest.keys())
                lost = set(before_keys) - set(after_keys)
                added = set(after_keys) - set(before_keys)
                if lost or added:
                    logging.info(
                        f"[ext-pool] normalize: -{sorted(lost)} +{sorted(added)}"
                    )
                with open(manifest_path, "w", encoding="utf-8") as f:
                    json.dump(manifest, f, ensure_ascii=False, indent=2)
            else:
                logging.warning(
                    f"[ext-pool] parse_manifest returned {{}} for {manifest_path} "
                    f"— see manifest.json.original for the raw input"
                )
                manifest = {}
        except Exception as e:
            logging.exception(f"[ext-pool] normalization failed for {manifest_path}: {e}")
            manifest = parse_manifest(manifest_path) or {}

        final = pool_path(ext_id)
        if os.path.exists(final):
            shutil.rmtree(final)
        shutil.move(tmp, final)
        tmp = None

        return ext_id, final, manifest
    finally:
        if tmp and os.path.exists(tmp):
            try: shutil.rmtree(tmp)
            except Exception: pass


def _inject_manifest_key(original_bytes: bytes, key_b64: str) -> bytes:
    """Insert a `key` field into manifest.json with minimal disturbance.
    Used when we have a CRX-derived pubkey and want to preserve every
    other byte of the original manifest verbatim (avoid parse + dump
    round-trip that may lose edge-case fields).

    Strategy: find the opening `{` and insert `"key": "<b64>",\\n  ` on
    the next line. If a `key` field already exists in the manifest,
    leave it alone (Chrome will use it).
    """
    text = original_bytes.decode("utf-8-sig", errors="replace")
    # If there's already a "key" field at the top level, don't double-add
    import re as _re
    if _re.search(r'"key"\s*:', text):
        return original_bytes
    # Find the first `{` and inject after it
    m = _re.search(r"\{", text)
    if not m:
        return original_bytes  # not valid JSON, give up
    inject = f'\n  "key": "{key_b64}",'
    patched = text[:m.end()] + inject + text[m.end():]
    return patched.encode("utf-8")


def add_from_unpacked_zip(zip_bytes: bytes,
                          source_filename: str = None) -> tuple[str, str, dict]:
    """Same as add_from_crx but for a plain ZIP of an unpacked
    extension folder. Useful when users have a developer-mode-style
    folder they want to import."""
    _ensure_pool_dir()
    import tempfile
    tmp = tempfile.mkdtemp(prefix="gs_ext_unpack_")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(tmp)

        # Some zips contain a single top-level folder rather than the
        # manifest at the root. Detect + descend if needed.
        if not os.path.exists(os.path.join(tmp, "manifest.json")):
            children = [d for d in os.listdir(tmp)
                        if os.path.isdir(os.path.join(tmp, d))]
            if len(children) == 1:
                inner = os.path.join(tmp, children[0])
                if os.path.exists(os.path.join(inner, "manifest.json")):
                    # Move inner contents up
                    for f in os.listdir(inner):
                        shutil.move(os.path.join(inner, f),
                                    os.path.join(tmp, f))
                    shutil.rmtree(inner)

        if not os.path.exists(os.path.join(tmp, "manifest.json")):
            raise ValueError("zip has no manifest.json at root or one level down")

        ext_id = _ensure_stable_key(tmp)
        manifest = parse_manifest(os.path.join(tmp, "manifest.json"))

        final = pool_path(ext_id)
        if os.path.exists(final):
            shutil.rmtree(final)
        shutil.move(tmp, final)
        tmp = None

        return ext_id, final, manifest
    finally:
        if tmp and os.path.exists(tmp):
            try: shutil.rmtree(tmp)
            except Exception: pass


# ────────────────────────────────────────────────────────────────
# Chrome Web Store install (download CRX directly)
# ────────────────────────────────────────────────────────────────

# Google's CRX update endpoint — this is the same URL Chrome itself
# uses to fetch extensions and their updates. The `prodversion` is
# our actual Chromium version (so the right CRX format / minimum-
# manifest-version variant gets served).
_CWS_UPDATE_URL = (
    "https://clients2.google.com/service/update2/crx"
    "?response=redirect&os=win&arch=x64&os_arch=x64&nacl_arch=x86-64"
    "&prod=chromium&prodchannel=stable&prodversion=147.0.7780.88"
    "&lang=en-US&acceptformat=crx2,crx3"
    "&x=id%3D{ext_id}%26installsource%3Dondemand%26uc"
)


def install_from_cws(extension_id_or_url: str) -> tuple[str, str, dict]:
    """Download an extension from the Chrome Web Store by ID or URL,
    install it into the pool. Returns (extension_id, pool_path,
    manifest_dict).
    """
    ext_id = _extract_cws_id(extension_id_or_url)
    if not ext_id:
        raise ValueError(
            f"can't extract extension id from {extension_id_or_url!r} -- "
            "expected 32-char id or chromewebstore.google.com URL"
        )

    import requests
    url = _CWS_UPDATE_URL.format(ext_id=ext_id)
    try:
        r = requests.get(url, timeout=30, allow_redirects=True)
    except Exception as e:
        raise RuntimeError(f"CWS download failed: {e}") from e

    if not r.ok:
        raise RuntimeError(
            f"CWS returned HTTP {r.status_code} for {ext_id} -- "
            f"is the id valid? Body: {r.text[:200]}"
        )
    if not r.content or len(r.content) < 100:
        raise RuntimeError(
            f"CWS returned empty body for {ext_id} -- "
            f"the extension may have been removed from the store"
        )

    return add_from_crx(r.content, source_url=f"cws:{ext_id}")


def _extract_cws_id(s: str) -> Optional[str]:
    """Pull the 32-char extension id out of either a bare id, a CWS
    URL like https://chromewebstore.google.com/detail/<slug>/<id> or
    /detail/<id>, or a URL with extra params."""
    s = (s or "").strip()
    if not s:
        return None
    # Bare id
    if len(s) == 32 and all(c in "abcdefghijklmnop" for c in s.lower()):
        return s.lower()
    # Try parsing as URL
    try:
        parsed = urllib.parse.urlparse(s)
        path = parsed.path or ""
        # /detail/<slug>/<id> or /detail/<id>
        parts = [p for p in path.split("/") if p]
        for p in reversed(parts):
            if len(p) == 32 and all(c in "abcdefghijklmnop" for c in p.lower()):
                return p.lower()
    except Exception:
        pass
    return None


# ────────────────────────────────────────────────────────────────
# DB-bridge convenience (so callers don't have to wire DB+pool manually)
# ────────────────────────────────────────────────────────────────

def install_and_register(crx_or_zip_bytes: bytes,
                         source: str,
                         source_url: str = None) -> dict:
    """Single entry point used by upload + CWS-install endpoints:
    unpack to pool, parse manifest, write DB row, return summary
    dict suitable for jsonify().

    `source` is one of: "manual_crx", "manual_unpacked", "cws".
    """
    from ghost_shell.db import get_db
    db = get_db()

    if source == "manual_unpacked":
        ext_id, pool_dir, manifest = add_from_unpacked_zip(
            crx_or_zip_bytes, source_filename=source_url,
        )
    else:
        ext_id, pool_dir, manifest = add_from_crx(
            crx_or_zip_bytes, source_url=source_url,
        )

    name = (manifest.get("name") or "").strip() or ext_id
    if name.startswith("__MSG_"):
        # Locale-string lookup: read default_locale messages file
        name = _resolve_locale_message(pool_dir, manifest, name) or ext_id

    desc = (manifest.get("description") or "").strip()
    if desc.startswith("__MSG_"):
        desc = _resolve_locale_message(pool_dir, manifest, desc) or ""

    icon_b64 = _icon_b64_from_pool(pool_dir, manifest)
    perms = _summarise_permissions(manifest)

    db.extension_create(
        ext_id              = ext_id,
        name                = name,
        description         = desc,
        version             = manifest.get("version"),
        source              = source,
        source_url          = source_url,
        pool_path           = pool_dir,
        manifest_json       = json.dumps(manifest, ensure_ascii=False),
        icon_b64            = icon_b64,
        permissions_summary = perms,
    )

    return {
        "ok":           True,
        "id":           ext_id,
        "name":         name,
        "description":  desc,
        "version":      manifest.get("version"),
        "pool_path":    pool_dir,
        "icon_b64":     icon_b64,
        "permissions":  perms,
    }


def _resolve_locale_message(pool_dir: str, manifest: dict,
                            msg_ref: str) -> Optional[str]:
    """If manifest uses __MSG_xxx__ for name/description, look up the
    actual string in _locales/<default_locale>/messages.json."""
    try:
        # __MSG_app_name__ → "app_name"
        key = msg_ref.replace("__MSG_", "").replace("__", "")
        default_locale = manifest.get("default_locale") or "en"
        loc_path = os.path.join(pool_dir, "_locales", default_locale, "messages.json")
        if not os.path.exists(loc_path):
            # Fallback: try en
            loc_path = os.path.join(pool_dir, "_locales", "en", "messages.json")
        if not os.path.exists(loc_path):
            return None
        with open(loc_path, "r", encoding="utf-8-sig") as f:
            messages = json.load(f)
        entry = messages.get(key) or messages.get(key.lower())
        if isinstance(entry, dict):
            return entry.get("message")
        if isinstance(entry, str):
            return entry
    except Exception:
        pass
    return None
