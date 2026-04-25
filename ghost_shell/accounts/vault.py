"""
vault.py — Credential vault unlocked with a user-chosen master password.

Design:
  - User picks a master password on first use ("initialize")
  - That password is run through PBKDF2-HMAC-SHA256 (200k iterations)
    with a per-install random salt, producing a 32-byte key
  - Key feeds a Fernet (AES-128-CBC + HMAC-SHA256) instance held ONLY
    in memory while the vault is "unlocked"
  - Locking the vault wipes the in-memory key
  - A short verification ciphertext is stored in config_kv so unlock()
    can confirm the master password without decrypting anything real

Persisted pieces (all in config_kv, never in plaintext):
  - vault.salt           : base64 random 16 bytes — key-derivation salt
  - vault.verifier       : Fernet(KNOWN_PLAINTEXT, key) — unlock check
  - vault.initialized_at : ISO timestamp

Not persisted (memory-only while unlocked):
  - The derived Fernet key. Lost on dashboard restart → user must unlock again.

External deps:
  cryptography >= 42   (standard Fernet + PBKDF2HMAC)
"""

from __future__ import annotations

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import os, base64, logging, threading
from datetime import datetime
from typing import Optional

try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

from ghost_shell.db import get_db


# Fixed plaintext for the unlock-verifier. Not secret; used like a
# password-hash check: decrypt it and compare to this known value.
_VERIFIER_PLAINTEXT = b"ghost_shell_vault_v1_ok"

_ITERATIONS = 200_000
_SALT_BYTES = 16


class VaultLockedError(RuntimeError):
    """Raised when decrypt/encrypt is attempted on a locked vault."""


class Vault:
    """Singleton per-process. Holds a Fernet in memory while unlocked."""

    def __init__(self):
        self._fernet: Optional[Fernet] = None
        self._lock = threading.RLock()

    # ─── Lifecycle ────────────────────────────────────────────

    @staticmethod
    def _ensure_crypto():
        if not HAS_CRYPTO:
            raise RuntimeError(
                "cryptography library is not installed. "
                "Install it with: pip install 'cryptography>=42'"
            )

    @staticmethod
    def is_initialized() -> bool:
        """True if a master password has ever been set."""
        db = get_db()
        return bool(db.config_get("vault.salt") and db.config_get("vault.verifier"))

    def is_unlocked(self) -> bool:
        with self._lock:
            return self._fernet is not None

    def initialize(self, master_password: str) -> None:
        """First-time setup — pick salt, derive key, write verifier.
        Also unlocks the vault in the same call."""
        self._ensure_crypto()
        if not master_password or len(master_password) < 4:
            raise ValueError("master password must be at least 4 characters")
        if self.is_initialized():
            raise RuntimeError(
                "vault already initialized — use unlock() instead. "
                "To reset, call reset() (destructive — encrypted data is lost)."
            )
        salt = os.urandom(_SALT_BYTES)
        key = self._derive_key(master_password, salt)
        fernet = Fernet(key)
        verifier = fernet.encrypt(_VERIFIER_PLAINTEXT).decode("ascii")

        db = get_db()
        db.config_set("vault.salt",           base64.b64encode(salt).decode("ascii"))
        db.config_set("vault.verifier",       verifier)
        db.config_set("vault.initialized_at", datetime.now().isoformat(timespec="seconds"))

        with self._lock:
            self._fernet = fernet
        logging.info("[vault] initialized and unlocked")

    def unlock(self, master_password: str) -> None:
        """Verify master and hold the Fernet in memory. Idempotent if
        already unlocked (re-verifies the password)."""
        self._ensure_crypto()
        db = get_db()
        salt_b64 = db.config_get("vault.salt")
        verifier = db.config_get("vault.verifier")
        if not salt_b64 or not verifier:
            raise RuntimeError("vault not initialized — call initialize() first")
        salt = base64.b64decode(salt_b64)
        key = self._derive_key(master_password, salt)
        fernet = Fernet(key)
        try:
            plain = fernet.decrypt(verifier.encode("ascii"))
        except InvalidToken:
            raise PermissionError("invalid master password")
        if plain != _VERIFIER_PLAINTEXT:
            raise PermissionError("verifier mismatch")
        with self._lock:
            self._fernet = fernet
        logging.info("[vault] unlocked")

    def lock(self) -> None:
        with self._lock:
            self._fernet = None
        logging.info("[vault] locked")

    def reset(self) -> None:
        """Destructive — clear master and all ciphertext. Use with care."""
        db = get_db()
        db.config_set("vault.salt", None)
        db.config_set("vault.verifier", None)
        db.config_set("vault.initialized_at", None)
        with self._lock:
            self._fernet = None
        # Drop all encrypted fields from accounts so they don't become
        # uninterpretable orphans. Caller can re-enter them after a fresh
        # initialize.
        conn = db._get_conn()
        with conn:
            conn.execute("UPDATE accounts SET password_enc = NULL, totp_secret_enc = NULL")
        logging.warning("[vault] reset — all encrypted fields cleared")

    # ─── Crypto ───────────────────────────────────────────────

    @staticmethod
    def _derive_key(master_password: str, salt: bytes) -> bytes:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=_ITERATIONS,
        )
        return base64.urlsafe_b64encode(kdf.derive(master_password.encode("utf-8")))

    def encrypt(self, plaintext: str) -> str:
        """Fernet-encrypt a string; returns base64 ciphertext."""
        if plaintext is None:
            return None
        with self._lock:
            if self._fernet is None:
                raise VaultLockedError("vault is locked")
            return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        if ciphertext is None:
            return None
        with self._lock:
            if self._fernet is None:
                raise VaultLockedError("vault is locked")
            try:
                return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
            except InvalidToken:
                raise RuntimeError("ciphertext invalid or encrypted under a different key")


# Module-level singleton
_vault = Vault()


def get_vault() -> Vault:
    return _vault
