"""
Generic credential / secret vault.

Kinds supported out of the box: account, email, social, crypto_wallet,
api_key, totp_only, note, custom. See ghost_shell.accounts.kinds for
the per-kind field schemas.

Public API:
    # Vault lifecycle
    from ghost_shell.accounts import Vault, get_vault, VaultLockedError

    # CRUD (generic)
    from ghost_shell.accounts import (
        add_item, update_item, list_items, get_item_cleartext,
        delete_item, set_status, totp_code,
    )

    # Kind metadata (for building forms)
    from ghost_shell.accounts.kinds import KINDS, list_kinds, get_kind

    # Legacy account-only wrappers
    from ghost_shell.accounts import (
        add_account, update_account, list_accounts,
        delete_account, set_account_status, get_account_cleartext,
    )
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

from .vault   import Vault, get_vault, VaultLockedError
from .totp    import compute_totp, remaining_seconds
from .kinds   import KINDS, list_kinds, get_kind

from .manager import (
    # Generic API (preferred)
    add_item,
    update_item,
    list_items,
    get_item_cleartext,
    delete_item,
    set_status,
    totp_code,
    # Legacy wrappers
    add_account,
    update_account,
    list_accounts,
    get_account_cleartext,
    delete_account,
    set_account_status,
)
