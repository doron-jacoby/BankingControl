"""Atomic per-account imports with checkpoints, stable aliases and local locking."""

import fcntl
import hashlib
import json
import os
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlcipher3 import dbapi2 as sqlcipher

from finance.models import (
    Account,
    ProviderTransaction,
    Transaction,
    TransactionStatus,
    require_aware,
    utc_now,
)
from finance.providers import FinanceProvider, ProviderError
from finance.repository import (
    dictionaries,
    save_account,
    save_record,
    transaction_from_row,
)
from finance.storage import StorageError, open_database


@contextmanager
def sync_lock(path: Path) -> Iterator[bool]:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(descriptor)


@dataclass
class SyncResult:
    status: str = "completed"
    accounts_processed: int = 0
    inserted: int = 0
    updated: int = 0
    started_at: str = field(default_factory=lambda: utc_now().isoformat())
    finished_at: str = ""
    errors: list[str] = field(default_factory=list)


def source_key(record: ProviderTransaction, occurrence: int = 0) -> str:
    if record.provider_transaction_id is not None:
        return "id:" + record.provider_transaction_id
    # Preserve multiplicity of identical ID-less rows using an occurrence ordinal.
    # ponytail: changed ID-less payloads need an explicit provider reconciliation
    # contract; guessing fuzzy matches can merge two legitimate purchases.
    amount = format(record.amount, "f")
    if "." in amount:
        amount = amount.rstrip("0").rstrip(".")
    values = [
        record.transaction_date.astimezone(UTC).isoformat(),
        amount,
        record.currency,
        record.merchant,
        record.original_description,
    ]
    digest = hashlib.sha256(json.dumps(values).encode()).hexdigest()
    return f"fingerprint:{digest}:{occurrence}"


def _upsert(
    db: sqlcipher.Connection,
    account: Account,
    record: ProviderTransaction,
    key: str,
    now: datetime,
) -> tuple[int, int]:
    alias = db.execute(
        "SELECT transaction_id FROM transaction_aliases "
        "WHERE account_id = ? AND source_key = ?",
        (account.internal_id, key),
    ).fetchone()
    pending_id = record.raw_metadata.get("pending_transaction_id")
    if alias is None and isinstance(pending_id, str):
        alias = db.execute(
            "SELECT transaction_id FROM transaction_aliases "
            "WHERE account_id = ? AND source_key = ?",
            (account.internal_id, "id:" + pending_id),
        ).fetchone()
    existing = None
    if alias:
        existing = transaction_from_row(
            dictionaries(
                db, "SELECT * FROM transactions WHERE internal_id = ?", (alias[0],)
            )[0]
        )
    values = asdict(record)
    values.pop("provider_account_id")
    values["transaction_date"] = record.transaction_date.astimezone(UTC)
    if existing:
        # An overlapping page can still include an obsolete pending representation.
        if (
            existing.status != TransactionStatus.PENDING
            and record.status == TransactionStatus.PENDING
        ):
            return 0, 0
        candidate = replace(existing, **values)
        changed = candidate != existing
        if changed:
            save_record(db, replace(candidate, updated_at=now))
        transaction_id = existing.internal_id
    else:
        candidate = Transaction(
            account_id=account.internal_id,
            provider=account.provider,
            created_at=now,
            updated_at=now,
            **values,
        )
        save_record(db, candidate)
        transaction_id = candidate.internal_id
        changed = False
    db.execute(
        "INSERT INTO transaction_aliases VALUES (?, ?, ?) "
        "ON CONFLICT(account_id, source_key) DO NOTHING",
        (account.internal_id, key, transaction_id),
    )
    return int(existing is None), int(changed)


class SyncService:
    def __init__(
        self, provider: FinanceProvider, path: Path, key: str, overlap_days: int = 7
    ) -> None:
        if not 0 <= overlap_days <= 365:
            raise ValueError("Overlap must be between zero and 365 days")
        self.provider = provider
        self.path = path
        self.key = key
        self.overlap_days = overlap_days

    def sync(self, *, now: datetime | None = None) -> SyncResult:
        now = now or utc_now()
        require_aware(now)
        result = SyncResult(started_at=now.isoformat())
        with sync_lock(self.path.with_suffix(".sync.lock")) as acquired:
            if not acquired:
                result.status = "already_running"
            else:
                try:
                    discovered = self.provider.list_accounts()
                    for account in discovered:
                        self._account(account, now, result)
                except ProviderError as error:
                    result.errors.append(error.code)
                except (ValueError, TypeError):
                    result.errors.append("validation")
        if result.errors:
            result.status = "partial" if result.accounts_processed else "failed"
        result.finished_at = utc_now().isoformat()
        return result

    def _account(self, incoming: Account, now: datetime, result: SyncResult) -> None:
        with open_database(self.key, self.path) as db:
            account = save_account(db, incoming)
            checkpoint = db.execute(
                "SELECT last_successful_sync FROM sync_states "
                "WHERE provider = ? AND account_id = ?",
                (account.provider, account.internal_id),
            ).fetchone()
            db.execute(
                "INSERT INTO sync_states(provider, account_id, last_sync_started_at, status) "
                "VALUES (?, ?, ?, 'running') ON CONFLICT(provider, account_id) "
                "DO UPDATE SET last_sync_started_at=excluded.last_sync_started_at, "
                "status='running', error=NULL",
                (account.provider, account.internal_id, now.isoformat()),
            )
        start = datetime.min.replace(tzinfo=UTC)
        if checkpoint and checkpoint[0]:
            start = datetime.fromisoformat(checkpoint[0]) - timedelta(
                days=self.overlap_days
            )
        try:
            records = self.provider.fetch_transactions(
                account.provider_account_id, start, now
            )
            if any(
                record.provider_account_id != account.provider_account_id
                or not start <= record.transaction_date <= now
                for record in records
            ):
                raise ProviderError("validation")
            inserted = updated = 0
            occurrences: Counter[str] = Counter()
            with open_database(self.key, self.path) as db:
                for record in sorted(
                    records, key=lambda r: r.status != TransactionStatus.PENDING
                ):
                    fingerprint = source_key(record)
                    ordinal = occurrences[fingerprint]
                    occurrences[fingerprint] += 1
                    added, changed = _upsert(
                        db, account, record, source_key(record, ordinal), now
                    )
                    inserted += added
                    updated += changed
                last_date = db.execute(
                    "SELECT max(transaction_date) FROM transactions WHERE account_id = ?",
                    (account.internal_id,),
                ).fetchone()[0]
                db.execute(
                    "UPDATE sync_states SET last_successful_sync=?, last_transaction_date=?, "
                    "last_sync_finished_at=?, status='succeeded', error=NULL, bootstrap_complete=1 "
                    "WHERE provider=? AND account_id=?",
                    (
                        now.isoformat(),
                        last_date,
                        utc_now().isoformat(),
                        account.provider,
                        account.internal_id,
                    ),
                )
            result.accounts_processed += 1
            result.inserted += inserted
            result.updated += updated
        except (ProviderError, StorageError, ValueError, TypeError) as error:
            code = (
                error.code
                if isinstance(error, ProviderError)
                else ("storage" if isinstance(error, StorageError) else "validation")
            )
            result.errors.append(code)
            with open_database(self.key, self.path) as db:
                db.execute(
                    "UPDATE sync_states SET status='failed', error=?, last_sync_finished_at=? "
                    "WHERE provider=? AND account_id=?",
                    (
                        code,
                        utc_now().isoformat(),
                        account.provider,
                        account.internal_id,
                    ),
                )
