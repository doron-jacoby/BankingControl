"""Auditable monthly totals, separated by currency, with no SQL floating sums."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from sqlcipher3 import dbapi2 as sqlcipher

from finance.models import Account, Transaction, TransactionStatus
from finance.repository import accounts, transactions


@dataclass
class AuditBucket:
    count: int = 0
    signed_amount: Decimal = Decimal("0")
    expense_effect: Decimal = Decimal("0")


@dataclass
class CurrencySummary:
    total: Decimal = Decimal("0")
    included_count: int = 0
    excluded_count: int = 0
    breakdown: dict[str, AuditBucket] = field(default_factory=dict)


@dataclass
class MonthlyExpenseSummary:
    year: int
    month: int
    timezone: str
    currencies: dict[str, CurrencySummary] = field(default_factory=dict)
    unclassified_count: int = 0


class AnalyticsService:
    def __init__(
        self, db: sqlcipher.Connection, timezone: str = "Asia/Jerusalem"
    ) -> None:
        self.db = db
        self.timezone = timezone

    def get_real_monthly_expenses(self, year: int, month: int) -> MonthlyExpenseSummary:
        zone = ZoneInfo(self.timezone)
        datetime(year, month, 1, tzinfo=zone)  # Validate before accessing records.
        if not self.db.in_transaction:
            self.db.execute("BEGIN")
        all_records = transactions(self.db)
        records = [
            tx
            for tx in all_records
            if (
                tx.transaction_date.astimezone(zone).year,
                tx.transaction_date.astimezone(zone).month,
            )
            == (year, month)
        ]
        account_map = {
            (account.provider, account.provider_account_id): account
            for account in accounts(self.db)
        }
        by_provider_id = {
            (tx.account_id, tx.provider_transaction_id): tx for tx in all_records
        }
        complete_accounts = {
            row[0]
            for row in self.db.execute(
                "SELECT account_id FROM sync_states WHERE bootstrap_complete=1"
            ).fetchall()
        }
        summary = MonthlyExpenseSummary(year, month, self.timezone)
        # Size precision to inputs, including carry digits, instead of rounding at
        # Decimal's ambient default (28). No exchange-rate conversions are guessed.
        precision = (
            max((max(0, tx.amount.adjusted() + 1) for tx in records), default=1)
            + max(
                (max(0, -int(tx.amount.as_tuple().exponent)) for tx in records),
                default=0,
            )
            + len(str(len(records)))
            + 4
        )
        with localcontext() as context:
            context.prec = max(28, precision)
            for tx in records:
                currency = summary.currencies.setdefault(tx.currency, CurrencySummary())
                if tx.classification_version == 0:
                    raise ValueError(
                        "Classify imported transactions before computing totals"
                    )
                reason, included = self._reason(
                    tx, records, account_map, complete_accounts, by_provider_id
                )
                bucket = currency.breakdown.setdefault(reason, AuditBucket())
                bucket.count += 1
                bucket.signed_amount += tx.amount
                if included:
                    bucket.expense_effect -= tx.amount
                    currency.total -= tx.amount
                    currency.included_count += 1
                else:
                    currency.excluded_count += 1
        return summary

    @staticmethod
    def _reason(
        tx: Transaction,
        records: list[Transaction],
        account_map: dict[tuple[str, str], Account],
        complete_accounts: set[str],
        by_provider_id: dict[tuple[str, str | None], Transaction],
    ) -> tuple[str, bool]:
        if tx.status == TransactionStatus.PENDING:
            return "pending", False
        if tx.status == TransactionStatus.REVERSED:
            return "reversed", False
        if tx.is_internal_transfer:
            return "internal_transfer", False
        if tx.is_investment:
            return "investment", False
        if tx.transaction_type == "technical":
            return "technical", False
        if tx.is_income:
            return "income", False
        if tx.transaction_type == "card_settlement":
            linked_id = tx.raw_metadata.get("settlement_account_id")
            linked = (
                account_map.get((tx.provider, linked_id))
                if isinstance(linked_id, str)
                else None
            )
            if (
                isinstance(linked, Account)
                and linked.account_type == "credit_card"
                and linked.provider == tx.provider
                and linked.internal_id in complete_accounts
                and any(
                    item.account_id == linked.internal_id
                    and item.status == TransactionStatus.POSTED
                    and item.currency == tx.currency
                    for item in records
                )
            ):
                return "card_settlement", False
            return "unmatched_card_settlement", tx.amount < 0
        reversal_id = tx.raw_metadata.get("reversal_of")
        original = (
            by_provider_id.get((tx.account_id, reversal_id))
            if isinstance(reversal_id, str)
            else None
        )
        if original and original.status == TransactionStatus.REVERSED:
            return "reversal_of_excluded_transaction", False
        if tx.is_refund and tx.amount > 0:
            return "refund", True
        if tx.amount < 0:
            return "expense", True
        return "other_credit", False
