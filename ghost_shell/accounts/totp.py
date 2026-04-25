"""
totp.py — Pure-stdlib RFC 6238 TOTP (HOTP with time-based counter).

Why no `pyotp`: keeping zero-extra-deps for the 2FA path.
RFC 6238 = RFC 4226 HOTP with counter = floor(UNIX_time / 30s).

Public API:
    compute_totp(secret_b32)  → 6-digit code
    remaining_seconds()       → how many seconds until the current code rolls
"""

from __future__ import annotations

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import base64, hmac, hashlib, struct, time


def _decode_base32(secret: str) -> bytes:
    """Google Authenticator / Authy / etc. export base32 WITHOUT padding.
    Tolerate padding + whitespace. Uppercase always — base32 is case-insensitive."""
    cleaned = (secret or "").replace(" ", "").replace("-", "").upper()
    # Pad to multiple of 8 (base32 block size)
    missing = (8 - len(cleaned) % 8) % 8
    cleaned += "=" * missing
    return base64.b32decode(cleaned, casefold=True)


def _hotp(secret_bytes: bytes, counter: int, digits: int = 6,
          algo: str = "sha1") -> str:
    """RFC 4226 HOTP."""
    h = hmac.new(secret_bytes, struct.pack(">Q", counter), getattr(hashlib, algo))
    digest = h.digest()
    offset = digest[-1] & 0x0F
    code = ((digest[offset]     & 0x7F) << 24 |
            (digest[offset + 1] & 0xFF) << 16 |
            (digest[offset + 2] & 0xFF) << 8  |
            (digest[offset + 3] & 0xFF))
    return str(code % (10 ** digits)).zfill(digits)


def compute_totp(secret_b32: str, *, digits: int = 6, period: int = 30,
                 at: float = None, algo: str = "sha1") -> str:
    """Current 6-digit TOTP code.

        secret_b32 : base32-encoded shared secret (standard QR export format)
        at         : unix timestamp; defaults to now()
    """
    t = int(at if at is not None else time.time())
    counter = t // period
    return _hotp(_decode_base32(secret_b32), counter, digits=digits, algo=algo)


def remaining_seconds(period: int = 30, at: float = None) -> int:
    """How many seconds until the current TOTP code rolls over."""
    t = int(at if at is not None else time.time())
    return period - (t % period)
