import secrets
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from test_foundation import NOW, account, provider_transaction

from finance.models import ProviderTransaction, TransactionStatus
from finance.providers import FakeFinanceProvider, ProviderError
from finance.repository import transactions
from finance.storage import open_database
from finance.sync import SyncService, source_key, sync_lock


class RecordingProvider(FakeFinanceProvider):
    fail_account: str | None = None

    def fetch_transactions(
        self, account_id: str, from_date: datetime, to_date: datetime
    ) -> list[ProviderTransaction]:
        self.last_start = from_date
        if account_id == self.fail_account:
            raise ProviderError("transient")
        return super().fetch_transactions(account_id, from_date, to_date)


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "finance.db"
        self.key = secrets.token_hex(32)
        with open_database(self.key, self.path, create=True):
            pass

    def test_initial_then_repeated_sync_is_idempotent_and_overlaps(self) -> None:
        provider = RecordingProvider([account()], [provider_transaction()])
        service = SyncService(provider, self.path, self.key)
        first = service.sync(now=NOW)
        self.assertEqual((first.status, first.inserted), ("completed", 1))
        self.assertEqual(provider.last_start.year, 1)
        second = service.sync(now=NOW + timedelta(days=1))
        self.assertEqual((second.inserted, second.updated), (0, 0))
        self.assertEqual(provider.last_start, NOW - timedelta(days=7))
        with open_database(self.key, self.path) as db:
            self.assertEqual(len(transactions(db)), 1)
            self.assertEqual(
                db.execute("SELECT bootstrap_complete FROM sync_states").fetchone()[0],
                1,
            )

    def test_idless_dedup_preserves_two_identical_purchases(self) -> None:
        tx = replace(provider_transaction(), provider_transaction_id=None)
        provider = FakeFinanceProvider([account()], [tx, tx])
        service = SyncService(provider, self.path, self.key)
        self.assertEqual(service.sync(now=NOW).inserted, 2)
        self.assertEqual(service.sync(now=NOW).inserted, 0)
        self.assertEqual(
            source_key(tx), source_key(replace(tx, amount=Decimal("-19.900")))
        )

    def test_pending_id_changes_and_old_pending_does_not_regress_final(self) -> None:
        pending = replace(provider_transaction(), status=TransactionStatus.PENDING)
        provider = FakeFinanceProvider([account()], [pending])
        SyncService(provider, self.path, self.key).sync(now=NOW)
        final = replace(
            pending,
            provider_transaction_id="final-id",
            status=TransactionStatus.POSTED,
            amount=Decimal("-20.00"),
            raw_metadata={"pending_transaction_id": pending.provider_transaction_id},
        )
        service = SyncService(
            FakeFinanceProvider([account()], [final, pending]), self.path, self.key
        )
        result = service.sync(now=NOW)
        self.assertEqual((result.inserted, result.updated), (0, 1))
        self.assertEqual(service.sync(now=NOW).updated, 0)
        with open_database(self.key, self.path) as db:
            saved = transactions(db)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].amount, Decimal("-20.00"))
        self.assertEqual(saved[0].status, TransactionStatus.POSTED)

    def test_failure_preserves_checkpoint_and_existing_data(self) -> None:
        provider = RecordingProvider([account()], [provider_transaction()])
        service = SyncService(provider, self.path, self.key)
        service.sync(now=NOW)
        provider.fail_account = "fake-account"
        result = service.sync(now=NOW + timedelta(days=1))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors, ["transient"])
        with open_database(self.key, self.path) as db:
            state = db.execute(
                "SELECT last_successful_sync, status, bootstrap_complete FROM sync_states"
            ).fetchone()
            self.assertEqual(state, (NOW.isoformat(), "failed", 1))
            self.assertEqual(len(transactions(db)), 1)

    def test_partial_account_failure_commits_only_successful_account(self) -> None:
        second = replace(account(), internal_id="second", provider_account_id="second")
        provider = RecordingProvider([account(), second], [provider_transaction()])
        provider.fail_account = "second"
        result = SyncService(provider, self.path, self.key).sync(now=NOW)
        self.assertEqual(
            (result.status, result.accounts_processed, result.inserted),
            ("partial", 1, 1),
        )
        with open_database(self.key, self.path) as db:
            state = db.execute(
                "SELECT last_successful_sync, bootstrap_complete FROM sync_states WHERE account_id='second'"
            ).fetchone()
            self.assertEqual(state, (None, 0))

    def test_rollback_when_later_record_cannot_be_persisted(self) -> None:
        good = provider_transaction()
        bad = replace(
            good,
            provider_transaction_id="bad",
            raw_metadata={"bad": object()},  # type: ignore[dict-item]
        )
        provider = FakeFinanceProvider([account()], [good, bad])
        result = SyncService(provider, self.path, self.key).sync(now=NOW)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.inserted, 0)
        with open_database(self.key, self.path) as db:
            self.assertEqual(len(transactions(db)), 0)
            self.assertIsNone(
                db.execute("SELECT last_successful_sync FROM sync_states").fetchone()[0]
            )

    def test_local_lock_returns_idempotent_result(self) -> None:
        service = SyncService(FakeFinanceProvider([account()], []), self.path, self.key)
        with sync_lock(self.path.with_suffix(".sync.lock")) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(service.sync(now=NOW).status, "already_running")
        self.assertEqual(service.sync(now=NOW).status, "completed")
