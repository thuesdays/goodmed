"""
kinds.py — Registry of vault-item kinds and the fields they carry.

Each kind has:
    key           : slug used in DB & API
    label         : human title
    icon          : emoji for UI chips
    identifier    : label + placeholder for the non-secret identifier
    secret_fields : list of {key, label, placeholder, kind: text|multiline}
                    — these live in the Fernet-encrypted JSON blob
    tip           : one-liner for the add-form help

Adding a new kind is one dict literal + (optional) UI preset button.
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

KINDS = {
    "account": {
        "label":      "Account (login + password)",
        "icon":       "👤",
        "identifier": {"label": "Username / email", "placeholder": "user@example.com"},
        "secret_fields": [
            {"key": "password",    "label": "Password",    "kind": "text"},
            {"key": "totp_secret", "label": "2FA secret (base32)", "kind": "text",
             "placeholder": "JBSWY3DPEHPK3PXP — optional"},
            {"key": "recovery",    "label": "Recovery codes",       "kind": "multiline",
             "placeholder": "one per line (optional)"},
        ],
        "tip": "Generic login — use this for most websites.",
    },

    "email": {
        "label":      "Email mailbox",
        "icon":       "✉",
        "identifier": {"label": "Email address", "placeholder": "me@example.com"},
        "secret_fields": [
            {"key": "password",    "label": "Password / App password", "kind": "text"},
            {"key": "imap_host",   "label": "IMAP host (optional)",    "kind": "text",
             "placeholder": "imap.gmail.com"},
            {"key": "smtp_host",   "label": "SMTP host (optional)",    "kind": "text",
             "placeholder": "smtp.gmail.com"},
            {"key": "totp_secret", "label": "2FA secret (optional)",   "kind": "text"},
        ],
        "tip": "Standalone mailbox — IMAP/SMTP hosts for programmatic reads.",
    },

    "social": {
        "label":      "Social network account",
        "icon":       "🌐",
        "identifier": {"label": "Username / handle", "placeholder": "@handle"},
        "secret_fields": [
            {"key": "password",    "label": "Password",    "kind": "text"},
            {"key": "totp_secret", "label": "2FA secret",  "kind": "text"},
            {"key": "session_cookie", "label": "Session cookie (optional)", "kind": "multiline",
             "placeholder": "raw cookie value — lets scripts skip login"},
        ],
        "tip": "Twitter / Facebook / Instagram / … — same shape as account, kept separate for filters.",
    },

    "crypto_wallet": {
        "label":      "Crypto wallet",
        "icon":       "💰",
        "identifier": {"label": "Wallet address / name", "placeholder": "0x… or my-hot-wallet"},
        "secret_fields": [
            {"key": "seed_phrase",     "label": "Seed phrase (BIP39)",  "kind": "multiline",
             "placeholder": "12 or 24 space-separated words"},
            {"key": "wallet_password", "label": "Wallet password",      "kind": "text"},
            {"key": "private_key",     "label": "Private key (0x…)",    "kind": "multiline"},
            {"key": "derivation_path", "label": "Derivation path",      "kind": "text",
             "placeholder": "m/44'/60'/0'/0/0 (optional)"},
        ],
        "tip": "Ethereum / Bitcoin / Solana / etc. Stored encrypted at rest — the master password is your only line of defense.",
    },

    "api_key": {
        "label":      "API key pair",
        "icon":       "🔑",
        "identifier": {"label": "Client ID / API label", "placeholder": "my-aws-dev"},
        "secret_fields": [
            {"key": "key",    "label": "Key / Access ID",     "kind": "text"},
            {"key": "secret", "label": "Secret / Secret key", "kind": "text"},
            {"key": "region", "label": "Region (optional)",   "kind": "text",
             "placeholder": "us-east-1"},
        ],
        "tip": "AWS / Stripe / OpenAI / any API that gives you a key-secret pair.",
    },

    "totp_only": {
        "label":      "TOTP secret (2FA only)",
        "icon":       "🔒",
        "identifier": {"label": "Label", "placeholder": "GitHub 2FA"},
        "secret_fields": [
            {"key": "totp_secret", "label": "Base32 secret", "kind": "text",
             "placeholder": "JBSWY3DPEHPK3PXP"},
        ],
        "tip": "A standalone authenticator code store — like a mini-Authy.",
    },

    "note": {
        "label":      "Secure note",
        "icon":       "📝",
        "identifier": {"label": "Title", "placeholder": "My backup PIN"},
        "secret_fields": [
            {"key": "body", "label": "Note contents", "kind": "multiline",
             "placeholder": "Free-form text — anything you want kept encrypted"},
        ],
        "tip": "Freeform encrypted text. Good for PINs, backup codes, prose.",
    },

    "custom": {
        "label":      "Custom (your own fields)",
        "icon":       "⚙",
        "identifier": {"label": "Identifier", "placeholder": "whatever makes sense"},
        "secret_fields": [],    # UI shows a key-value dynamic editor for this kind
        "tip": "Build your own field set. The UI gives you a key-value editor.",
    },
}


def get_kind(key: str) -> dict | None:
    return KINDS.get(key)


def list_kinds() -> list[dict]:
    """UI-friendly summary: [{key, label, icon, tip}]."""
    return [
        {"key": k, "label": v["label"], "icon": v["icon"], "tip": v["tip"]}
        for k, v in KINDS.items()
    ]
