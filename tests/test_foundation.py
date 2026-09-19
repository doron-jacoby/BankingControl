import secrets
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from finance.models import (
    Account,
    ProviderTransaction,
    SyncState,
    Transaction,
)
from finance.providers import FakeFinanceProvider, FinanceProvider
from finance.security import (
    DATABASE_KEY,
    SERVICE,
    FakeSecretStore,
    MacOSKeychain,
    SecretError,
    create_database_key,
    load_database_key,
)
from finance.storage import StorageError, open_database

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def account() -> Account:
    return Account(
        internal_id="local-account",
        provider="fake",
        provider_account_id="fake-account",
        institution="Demo institution",
        account_type="checking",
        display_name="Demo account",
        currency="ILS",
    )


def provider_transaction() -> ProviderTransaction:
    return ProviderTransaction(
        provider_account_id="fake-account",
        provider_transaction_id="fake-transaction",
        transaction_date=NOW,
        amount=Decimal("-19.90"),
        currency="ILS",
        original_description="Synthetic purchase",
    )


class ModelTests(unittest.TestCase):
    def test_money_rejects_float_and_nonfinite_values(self) -> None:
        for invalid in (19.9, Decimal("NaN"), Decimal("Infinity")):
            with (
                self.subTest(kind=type(invalid).__name__),
                self.assertRaises(ValueError),
            ):
                replace(provider_transaction(), amount=invalid)  # type: ignore[arg-type]

    def test_currency_time_and_status_validation(self) -> None:
        with self.assertRaises(ValueError):
            replace(account(), currency="ils")
        with self.assertRaises(ValueError):
            replace(provider_transaction(), transaction_date=NOW.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            replace(provider_transaction(), status="unknown")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            SyncState(provider="fake", account_id="a", error="secret")  # type: ignore[arg-type]

    def test_models_do_not_expose_records_in_repr(self) -> None:
        tx = Transaction(
            account_id="local-account",
            provider="fake",
            provider_transaction_id="tx",
            transaction_date=NOW,
            amount=Decimal("-0.10"),
            currency="ILS",
            original_description="private description",
        )
        self.assertNotIn("private description", repr(tx))
        self.assertNotIn("Demo institution", repr(account()))


class ProviderTests(unittest.TestCase):
    def test_account_filter_and_inclusive_range(self) -> None:
        tx = provider_transaction()
        acc = account()
        provider: FinanceProvider = FakeFinanceProvider(
            [acc], [tx, replace(tx, transaction_date=NOW + timedelta(days=1))]
        )
        self.assertEqual(provider.fetch_transactions("fake-account", NOW, NOW), [tx])
        self.assertEqual(provider.list_accounts(), [acc])

    def test_fake_returns_independent_metadata(self) -> None:
        provider = FakeFinanceProvider([account()], [provider_transaction()])
        provider.list_accounts()[0].metadata["test"] = "mutation"
        self.assertEqual(provider.list_accounts()[0].metadata, {})
        with self.assertRaises(ValueError):
            provider.fetch_transactions("unknown", NOW, NOW)
        with self.assertRaises(ValueError):
            provider.fetch_transactions("fake-account", NOW, NOW - timedelta(days=1))

    def test_fake_rejects_orphan_records(self) -> None:
        with self.assertRaises(ValueError):
            FakeFinanceProvider([], [provider_transaction()])


class KeychainTests(unittest.TestCase):
    def test_create_load_and_no_overwrite(self) -> None:
        store = FakeSecretStore()
        key = create_database_key(store)
        self.assertEqual(len(bytes.fromhex(key)), 32)
        self.assertEqual(load_database_key(store), key)
        with self.assertRaises(SecretError):
            create_database_key(store)
        self.assertEqual(load_database_key(store), key)

    def test_missing_or_invalid_key_fails_closed(self) -> None:
        store = FakeSecretStore()
        with self.assertRaises(SecretError):
            load_database_key(store)
        self.assertIsNone(store.get_password(SERVICE, DATABASE_KEY))
        store.set_password(SERVICE, DATABASE_KEY, "invalid")
        with self.assertRaises(SecretError):
            load_database_key(store)

    def test_macos_backend_with_fake_does_not_access_keychain(self) -> None:
        with (
            patch("finance.security.sys.platform", "darwin"),
            patch("keyring.backends.macOS.Keyring", return_value=FakeSecretStore()),
        ):
            adapter = MacOSKeychain()
            self.assertEqual(load_database_key_after_creation(adapter), 64)

    def test_backend_failures_are_sanitized(self) -> None:
        with (
            patch("finance.security.sys.platform", "darwin"),
            patch("keyring.backends.macOS.Keyring") as backend,
        ):
            backend.return_value.get_password.side_effect = RuntimeError(
                "private-token"
            )
            with self.assertRaises(SecretError) as raised:
                MacOSKeychain().get_password(SERVICE, DATABASE_KEY)
            self.assertNotIn("private-token", str(raised.exception))


def load_database_key_after_creation(store: MacOSKeychain) -> int:
    create_database_key(store)
    return len(load_database_key(store))


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / "finance.db"
        self.key = secrets.token_hex(32)

    def test_encrypted_creation_reopening_permissions_and_plaintext_rejection(
        self,
    ) -> None:
        with open_database(self.key, self.path, create=True) as db:
            self.assertTrue(db.execute("PRAGMA cipher_version").fetchone()[0])
            self.assertEqual(db.execute("PRAGMA cipher_use_hmac").fetchone()[0], "1")
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            db.execute("CREATE TABLE probe (amount DECIMAL_TEXT, description TEXT)")
            db.execute(
                "INSERT INTO probe VALUES (?, ?)",
                (Decimal("12345678901234567890.123456789"), "private-test-marker"),
            )
        data = self.path.read_bytes()
        self.assertNotIn(b"SQLite format 3", data)
        self.assertNotIn(b"private-test-marker", data)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        with open_database(self.key, self.path) as db:
            self.assertEqual(
                db.execute("SELECT amount FROM probe").fetchone()[0],
                Decimal("12345678901234567890.123456789"),
            )
            self.assertEqual(db.execute("PRAGMA cipher_integrity_check").fetchall(), [])
        plain = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                plain.execute("SELECT * FROM sqlite_master").fetchall()
        finally:
            plain.close()

    def test_wrong_key_does_not_change_database(self) -> None:
        with open_database(self.key, self.path, create=True):
            pass
        before = self.path.read_bytes()
        with (
            self.assertRaises(StorageError),
            open_database(secrets.token_hex(32), self.path),
        ):
            pass
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_database_and_duplicate_create_do_not_replace_data(self) -> None:
        with self.assertRaises(StorageError), open_database(self.key, self.path):
            pass
        self.assertFalse(self.path.exists())
        with open_database(self.key, self.path, create=True):
            pass
        before = self.path.read_bytes()
        with (
            self.assertRaises(StorageError),
            open_database(self.key, self.path, create=True),
        ):
            pass
        self.assertEqual(self.path.read_bytes(), before)

    def test_rollback_and_float_rejection(self) -> None:
        with open_database(self.key, self.path, create=True) as db:
            db.execute("CREATE TABLE probe (amount DECIMAL_TEXT)")
        with self.assertRaises(RuntimeError), open_database(self.key, self.path) as db:
            db.execute("INSERT INTO probe VALUES (?)", (Decimal("0.1"),))
            raise RuntimeError("synthetic failure")
        with open_database(self.key, self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM probe").fetchone()[0], 0)
            with self.assertRaises(ValueError):
                db.execute("INSERT INTO probe VALUES (?)", (0.1,))

    def test_symlink_rejected(self) -> None:
        with open_database(self.key, self.path, create=True):
            pass
        linked = self.path.parent / "linked.db"
        linked.symlink_to(self.path)
        with self.assertRaises(StorageError), open_database(self.key, linked):
            pass


if __name__ == "__main__":
    unittest.main()
