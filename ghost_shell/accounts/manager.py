"""
manager.py — CRUD layer that encrypts/decrypts via the vault.

Secrets are stored as a Fernet-encrypted JSON blob. Which keys live in
the blob depends on the item's `kind` — see ghost_shell/accounts/kinds.py.

All encryption + decryption happens HERE so the DB layer never sees
plaintext and callers (API / scripts) never touch Fernet directly.

The vault must be unlocked for any function that takes plaintext
sensitive input OR returns decrypted output — those raise
VaultLockedError otherwise.
"""

from __future__ import annotations

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import json
from typing import Optional

from ghost_shell.db import get_db
from .vault import get_vault, VaultLockedError
from .totp  import compute_totp as _compute_totp, remaining_seconds as _totp_remaining


# ── Encoding helpers ───────────────────────────────────────────

def _encrypt_secrets(secrets: dict | None) -> Optional[str]:
    """Serialize the secrets dict → JSON → Fernet. None / empty → None."""
    if not secrets:
        return None
    # Drop explicit None entries so an untouched field doesn't bloat the blob
    clean = {k: v for k, v in secrets.items() if v not in (None, "")}
    if not clean:
        return None
    return get_vault().encrypt(json.dumps(clean, ensure_ascii=False))


def _decrypt_secrets(ciphertext: str | None) -> dict:
    if not ciphertext:
        return {}
    raw = get_vault().decrypt(ciphertext)
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        # Legacy: if ciphertext is a bare string (not JSON), wrap it
        return {"value": raw}


# ── CRUD ───────────────────────────────────────────────────────

def add_item(*, name: str, kind: str = "account",
             service: str = None, identifier: str = None,
             secrets: dict = None,
             profile_name: str = None, status: str = "active",
             tags: list = None, notes: str = None) -> int:
    """Create a new vault item. `secrets` is a plain dict; we encrypt it."""
    enc = _encrypt_secrets(secrets)
    return get_db().vault_add(
        name=name, kind=kind, service=service, identifier=identifier,
        secrets_enc=enc, profile_name=profile_name, status=status,
        tags=tags, notes=notes,
    )


def update_item(item_id: int, *,
                name: str = None, kind: str = None,
                service: str = None, identifier: str = None,
                secrets: Optional[dict] = ...,   # ... = don't touch; None = clear
                profile_name: str = None,
                status: str = None,
                tags: list = None,
                notes: str = None) -> bool:
    """Partial update. Sentinel `...` = leave untouched; None = clear."""
    fields: dict = {}
    if name is not None:         fields["name"]         = name
    if kind is not None:         fields["kind"]         = kind
    if service is not None:      fields["service"]      = service
    if identifier is not None:   fields["identifier"]   = identifier
    if profile_name is not None: fields["profile_name"] = profile_name
    if status is not None:       fields["status"]       = status
    if notes is not None:        fields["notes"]        = notes
    if tags is not None:         fields["tags"]         = tags

    if secrets is not ...:
        fields["secrets_enc"] = _encrypt_secrets(secrets) if secrets else None

    if not fields:
        return False
    return get_db().vault_update(item_id, **fields)


def list_items(kind: str = None, service: str = None, status: str = None,
               profile_name: str = None, search: str = None) -> list[dict]:
    """Metadata-only — no ciphertext, no decryption. Vault can be locked."""
    return get_db().vault_list(
        kind=kind, service=service, status=status,
        profile_name=profile_name, search=search,
    )


def get_item_cleartext(item_id: int) -> Optional[dict]:
    """Full record with `secrets` field decrypted. Requires unlocked vault."""
    row = get_db().vault_get(item_id)
    if not row:
        return None
    out = dict(row)
    out["secrets"] = _decrypt_secrets(row.get("secrets_enc"))
    out.pop("secrets_enc", None)
    return out


def delete_item(item_id: int) -> bool:
    return get_db().vault_delete(item_id)


def set_status(item_id: int, status: str, login_status: str = None) -> bool:
    return get_db().vault_set_status(item_id, status, login_status)


# ── TOTP helper ────────────────────────────────────────────────

def totp_code(item_id: int) -> Optional[dict]:
    """Compute the current 6-digit TOTP code for an item that carries
    `secrets.totp_secret`. Returns None if no secret present. Vault
    must be unlocked."""
    row = get_db().vault_get(item_id)
    if not row:
        return None
    secrets = _decrypt_secrets(row.get("secrets_enc"))
    secret  = secrets.get("totp_secret")
    if not secret:
        return None
    return {
        "code":      _compute_totp(secret),
        "remaining": _totp_remaining(),
    }


# ── Legacy aliases (pre-rename callers) ────────────────────────
# The old API used account_* naming. Keep thin wrappers until callers
# migrate. These map the flat password + totp_secret fields to the new
# nested `secrets` dict shape.

def add_account(*, name, service, login=None, password=None, totp_secret=None,
                profile_name=None, status="active", notes=None):
    secrets = {"password": password, "totp_secret": totp_secret}
    return add_item(name=name, kind="account", service=service,
                    identifier=login, secrets=secrets,
                    profile_name=profile_name, status=status, notes=notes)


def update_account(account_id, *, name=None, service=None, login=None,
                   password=..., totp_secret=..., profile_name=None,
                   status=None, notes=None):
    # Fetch current secrets, patch, re-save
    row = get_db().vault_get(account_id)
    current = _decrypt_secrets(row.get("secrets_enc")) if row and row.get("secrets_enc") else {}
    if password    is not ...: current["password"]    = password
    if totp_secret is not ...: current["totp_secret"] = totp_secret
    secrets = current if any(current.values()) else None
    return update_item(account_id, name=name, service=service,
                       identifier=login, secrets=secrets,
                       profile_name=profile_name, status=status, notes=notes)


list_accounts          = list_items
delete_account         = delete_item
set_account_status     = set_status


def get_account_cleartext(account_id: int) -> Optional[dict]:
    """Flat-shape cleartext dict for legacy callers — exposes
    `password` + `totp_secret` at the top level."""
    full = get_item_cleartext(account_id)
    if not full:
        return None
    s = full.get("secrets") or {}
    full["password"]    = s.get("password")
    full["totp_secret"] = s.get("totp_secret")
    return full
