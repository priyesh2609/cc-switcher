"""Account registry and settings, persisted as private JSON files.

Metadata only. Tokens never touch these files; they live in Keychain.
These functions do NOT take ``app_lock``; callers performing read-modify-write
sequences must hold it.
"""

from __future__ import annotations

from cc_switcher import paths
from cc_switcher.fsutil import atomic_write_json, ensure_private_dir, read_json
from cc_switcher.models import Account, Settings

SCHEMA_VERSION = 1


class AccountNotFound(LookupError):
    pass


class AmbiguousAccount(LookupError):
    pass


def load_accounts() -> list[Account]:
    data = read_json(paths.accounts_file(), {})
    rows = data.get("accounts", []) if isinstance(data, dict) else []
    accounts: list[Account] = []
    for row in rows:
        if isinstance(row, dict):
            try:
                accounts.append(Account.from_dict(row))
            except TypeError:
                continue
    return accounts


def save_accounts(accounts: list[Account]) -> None:
    ensure_private_dir(paths.app_dir())
    atomic_write_json(
        paths.accounts_file(),
        {"version": SCHEMA_VERSION, "accounts": [a.to_dict() for a in accounts]},
    )


def get_account(account_id: str) -> Account | None:
    return next((a for a in load_accounts() if a.id == account_id), None)


def upsert_account(account: Account) -> None:
    accounts = [a for a in load_accounts() if a.id != account.id]
    accounts.append(account)
    save_accounts(accounts)


def remove_account(account_id: str) -> None:
    save_accounts([a for a in load_accounts() if a.id != account_id])


def find_account(query: str) -> Account:
    """Resolve a user-typed reference: exact id, label, or email (case-insensitive),
    then a unique prefix of label/email. Raises AccountNotFound / AmbiguousAccount.
    """
    q = query.strip().lower()
    accounts = load_accounts()
    for predicate in (
        lambda a: a.id.lower() == q,
        lambda a: a.label.lower() == q,
        lambda a: a.email.lower() == q,
    ):
        matches = [a for a in accounts if predicate(a)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AmbiguousAccount(
                f"'{query}' matches several accounts: "
                + ", ".join(f"{a.email} ({a.organization_name or a.id})" for a in matches)
                + ". Give each a label with `ccs label`."
            )
    prefix = [a for a in accounts if a.label.lower().startswith(q) or a.email.lower().startswith(q)]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        raise AmbiguousAccount(f"'{query}' matches several accounts: " + ", ".join(a.name for a in prefix))
    raise AccountNotFound(f"No saved account matches '{query}'. Run `ccs list`.")


def load_settings() -> Settings:
    data = read_json(paths.settings_file(), {})
    return Settings.from_dict(data) if isinstance(data, dict) else Settings()


def save_settings(settings: Settings) -> None:
    ensure_private_dir(paths.app_dir())
    atomic_write_json(paths.settings_file(), settings.to_dict())
