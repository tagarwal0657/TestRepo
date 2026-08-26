"""Ways to build the list of sub-accounts that should receive a release."""

from __future__ import annotations

import csv
import json
import os
from typing import Iterable, List, Optional, Sequence, Set

from .client import BoomiClient
from .errors import ConfigError
from .models import SubAccount

ACTIVE_ACCOUNT_STATUSES = frozenset({"active", "trial", "unlimited"})


def discover_sub_accounts(
    client: BoomiClient,
    *,
    only_active: bool = True,
) -> List[SubAccount]:
    """Query the Account object for sub-accounts created by the parent account."""
    accounts: List[SubAccount] = []
    for payload in client.query_accounts(exclude_deleted=True):
        account = SubAccount.from_api(payload)
        if not account.account_id:
            continue
        if account.account_id == client.account_id:
            continue  # the parent account itself is not a target
        status = str(payload.get("status") or "").strip().casefold()
        if only_active and status and status not in ACTIVE_ACCOUNT_STATUSES:
            continue
        accounts.append(account)
    return accounts


def load_sub_accounts_file(path: str) -> List[SubAccount]:
    """Read sub-accounts from a ``.json``, ``.csv`` or newline-delimited file."""
    if not os.path.exists(path):
        raise ConfigError(f"Sub-account file not found: {path}")
    extension = os.path.splitext(path)[1].casefold()
    with open(path, "r", encoding="utf-8") as handle:
        if extension == ".json":
            return _parse_json_accounts(json.load(handle), path)
        if extension == ".csv":
            return _parse_csv_accounts(handle)
        return _parse_text_accounts(handle)


def _parse_json_accounts(payload: object, path: str) -> List[SubAccount]:
    if isinstance(payload, dict):
        payload = payload.get("accounts", payload.get("result", []))
    if not isinstance(payload, list):
        raise ConfigError(f"Expected a JSON list of sub-accounts in {path}")
    accounts: List[SubAccount] = []
    for entry in payload:
        if isinstance(entry, str):
            account_id, name = entry.strip(), None
        elif isinstance(entry, dict):
            account_id = str(entry.get("accountId") or entry.get("account_id") or "").strip()
            name = entry.get("name")
            name = str(name).strip() if name else None
        else:
            raise ConfigError(f"Unsupported sub-account entry in {path}: {entry!r}")
        if account_id:
            accounts.append(SubAccount(account_id=account_id, name=name))
    return accounts


def _parse_csv_accounts(handle: Iterable[str]) -> List[SubAccount]:
    accounts: List[SubAccount] = []
    for row in csv.reader(handle):
        if not row:
            continue
        account_id = row[0].strip()
        if not account_id or account_id.startswith("#"):
            continue
        if account_id.casefold() in {"accountid", "account_id"}:
            continue  # header row
        name = row[1].strip() if len(row) > 1 and row[1].strip() else None
        accounts.append(SubAccount(account_id=account_id, name=name))
    return accounts


def _parse_text_accounts(handle: Iterable[str]) -> List[SubAccount]:
    accounts: List[SubAccount] = []
    for line in handle:
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        accounts.append(SubAccount(account_id=entry))
    return accounts


def resolve_sub_accounts(
    client: BoomiClient,
    *,
    explicit: Optional[Sequence[str]] = None,
    file_path: Optional[str] = None,
    discover: bool = False,
    exclude: Optional[Iterable[str]] = None,
    only_active: bool = True,
) -> List[SubAccount]:
    """Merge every configured sub-account source into one de-duplicated list."""
    collected: List[SubAccount] = []
    if explicit:
        collected.extend(SubAccount(account_id=value.strip()) for value in explicit if value.strip())
    if file_path:
        collected.extend(load_sub_accounts_file(file_path))
    if discover:
        collected.extend(discover_sub_accounts(client, only_active=only_active))
    if not collected:
        raise ConfigError(
            "No sub-accounts selected. Use --sub-account, --sub-accounts-file or --discover."
        )

    excluded: Set[str] = {value.strip() for value in (exclude or ()) if value.strip()}
    seen: Set[str] = set()
    unique: List[SubAccount] = []
    for account in collected:
        if account.account_id in excluded or account.account_id in seen:
            continue
        seen.add(account.account_id)
        unique.append(account)
    return unique
