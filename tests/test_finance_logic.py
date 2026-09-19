import secrets
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from test_foundation import NOW, account, provider_transaction

from finance.analytics import AnalyticsService
from finance.classification import RuleClassifier, backfill, save_rule
from finance.demo import demo_provider
from finance.models import Category, ClassificationRule, Transaction, TransactionStatus
from finance.providers import FakeFinanceProvider
from finance.repository import transactions
from finance.service import FinanceService
from finance.storage import open_database
from finance.sync import SyncService


class FinanceLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "finance.db"
        self.key = secrets.token_hex(32)
        with open_database(self.key, self.path, create=True):
            pass

    def test_demo_totals_and_every_audit_bucket(self) -> None:
        FinanceService(SyncService(demo_provider(NOW), self.path, self.key)).sync(
            now=NOW
        )
        with open_database(self.key, self.path) as db:
            summary = AnalyticsService(db).get_real_monthly_expenses(2026, 9)
        ils = summary.currencies["ILS"]
        self.assertEqual(ils.total, Decimal("419.90"))
        self.assertEqual(summary.currencies["USD"].total, Decimal("10.00"))
        self.assertEqual((ils.included_count, ils.excluded_count), (4, 8))
        expected = {
            "expense": 3,
            "refund": 1,
            "card_settlement": 1,
            "internal_transfer": 2,
            "investment": 1,
            "income": 1,
            "technical": 1,
            "pending": 1,
            "reversed": 1,
        }
        self.assertEqual(
            {name: item.count for name, item in ils.breakdown.items()}, expected
        )
        self.assertEqual(
            sum(item.expense_effect for item in ils.breakdown.values()), ils.total
        )

    def test_manual_priority_overrides_known_merchant_and_is_recomputable(self) -> None:
        FinanceService(SyncService(demo_provider(NOW), self.path, self.key)).sync(
            now=NOW
        )
        with open_database(self.key, self.path) as db:
            save_rule(
                db,
                ClassificationRule(
                    match_type="merchant_exact",
                    match_value="Demo market",
                    category=Category.SHOPPING,
                    priority=1,
                ),
            )
            save_rule(
                db,
                ClassificationRule(
                    match_type="merchant_exact",
                    match_value="Demo market",
                    category=Category.HEALTH,
                    priority=10,
                ),
            )
            self.assertEqual(backfill(db), 2)
            saved = [tx for tx in transactions(db) if tx.merchant == "Demo market"]
            self.assertTrue(all(tx.category == Category.HEALTH for tx in saved))
            self.assertTrue(all(tx.category_source == "manual" for tx in saved))
            self.assertTrue(next(tx for tx in saved if tx.amount > 0).is_refund)
            self.assertEqual(backfill(db), 0)

    def test_backfill_from_date_leaves_older_records_unchanged(self) -> None:
        old = replace(
            provider_transaction(),
            transaction_date=NOW - timedelta(days=30),
            provider_transaction_id="old",
        )
        SyncService(
            FakeFinanceProvider([account()], [old, provider_transaction()]),
            self.path,
            self.key,
        ).sync(now=NOW)
        with open_database(self.key, self.path) as db:
            self.assertEqual(backfill(db, NOW), 1)
            saved = transactions(db)
            self.assertEqual([tx.classification_version for tx in saved], [0, 1])

    def test_classification_failure_does_not_undo_import(self) -> None:
        service = FinanceService(
            SyncService(
                FakeFinanceProvider([account()], [provider_transaction()]),
                self.path,
                self.key,
            )
        )
        with patch("finance.service.backfill", side_effect=ValueError("synthetic")):
            result = service.sync(now=NOW)
        self.assertEqual(result.status, "partial")
        with open_database(self.key, self.path) as db:
            self.assertEqual(len(transactions(db)), 1)
            self.assertEqual(
                db.execute("SELECT last_successful_sync FROM sync_states").fetchone()[
                    0
                ],
                NOW.isoformat(),
            )
            with self.assertRaises(ValueError):
                AnalyticsService(db).get_real_monthly_expenses(2026, 9)

    def test_unlinked_settlement_is_not_silently_excluded(self) -> None:
        tx = replace(provider_transaction(), transaction_type="card_settlement")
        FinanceService(
            SyncService(FakeFinanceProvider([account()], [tx]), self.path, self.key)
        ).sync(now=NOW)
        with open_database(self.key, self.path) as db:
            summary = AnalyticsService(db).get_real_monthly_expenses(2026, 9)
        self.assertEqual(summary.currencies["ILS"].total, Decimal("19.90"))
        self.assertEqual(
            summary.currencies["ILS"].breakdown["unmatched_card_settlement"].count, 1
        )

    def test_reversed_original_and_linked_credit_net_to_zero(self) -> None:
        original = replace(provider_transaction(), status=TransactionStatus.REVERSED)
        credit = replace(
            original,
            provider_transaction_id="credit",
            status=TransactionStatus.POSTED,
            amount=Decimal("19.90"),
            transaction_type="reversal",
            raw_metadata={"reversal_of": original.provider_transaction_id},
        )
        FinanceService(
            SyncService(
                FakeFinanceProvider([account()], [original, credit]),
                self.path,
                self.key,
            )
        ).sync(now=NOW)
        with open_database(self.key, self.path) as db:
            summary = AnalyticsService(db).get_real_monthly_expenses(2026, 9)
        self.assertEqual(summary.currencies["ILS"].total, Decimal(0))
        self.assertEqual(summary.currencies["ILS"].excluded_count, 2)

    def test_decimal_sums_do_not_round_at_default_context_precision(self) -> None:
        huge = replace(
            provider_transaction(), amount=Decimal("-1000000000000000000000000000000")
        )
        tiny = replace(
            provider_transaction(),
            provider_transaction_id="tiny",
            amount=Decimal("-0.000000000000000000000000000001"),
        )
        FinanceService(
            SyncService(
                FakeFinanceProvider([account()], [huge, tiny]), self.path, self.key
            )
        ).sync(now=NOW)
        with open_database(self.key, self.path) as db:
            total = (
                AnalyticsService(db)
                .get_real_monthly_expenses(2026, 9)
                .currencies["ILS"]
                .total
            )
        self.assertEqual(
            total,
            Decimal("1000000000000000000000000000000.000000000000000000000000000001"),
        )

    def test_month_boundary_uses_selected_timezone(self) -> None:
        tx = replace(
            provider_transaction(),
            transaction_date=NOW.replace(day=1, hour=0) - timedelta(hours=1),
        )
        FinanceService(
            SyncService(FakeFinanceProvider([account()], [tx]), self.path, self.key)
        ).sync(now=NOW)
        with open_database(self.key, self.path) as db:
            self.assertEqual(
                AnalyticsService(db, "UTC")
                .get_real_monthly_expenses(2026, 9)
                .currencies,
                {},
            )
            self.assertEqual(
                AnalyticsService(db)
                .get_real_monthly_expenses(2026, 9)
                .currencies["ILS"]
                .total,
                Decimal("19.90"),
            )

    def test_disabled_rule_and_explicit_flag_override(self) -> None:
        transaction = Transaction(
            account_id="a",
            provider="fake",
            provider_transaction_id="tx",
            transaction_date=NOW,
            amount=Decimal("-1"),
            currency="ILS",
            original_description="demo",
            transaction_type="investment",
        )
        disabled = ClassificationRule(
            match_type="transaction_type",
            match_value="investment",
            category=Category.OTHER,
            enabled=False,
        )
        self.assertTrue(RuleClassifier([disabled]).classify(transaction).is_investment)
        override = replace(disabled, enabled=True, flags={"is_investment": False})
        self.assertFalse(RuleClassifier([override]).classify(transaction).is_investment)
