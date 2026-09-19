"""Synthetic monthly scenario; all names, identifiers and amounts are invented."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from finance.models import Account, ProviderTransaction, TransactionStatus, utc_now
from finance.providers import FakeFinanceProvider


def demo_provider(now: datetime | None = None) -> FakeFinanceProvider:
    now = now or utc_now()
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    bank = Account(
        internal_id="demo-bank",
        provider="fake",
        provider_account_id="demo-bank",
        institution="Synthetic institution",
        account_type="checking",
        display_name="DEMO checking",
        currency="ILS",
    )
    card = replace(
        bank,
        internal_id="demo-card",
        provider_account_id="demo-card",
        account_type="credit_card",
        display_name="DEMO card",
    )
    savings = replace(
        bank,
        internal_id="demo-savings",
        provider_account_id="demo-savings",
        display_name="DEMO savings",
    )
    records = []
    examples = [
        ("groceries", "demo-card", "-250.00", "Demo market", "purchase", "ILS"),
        ("restaurant", "demo-card", "-120.00", "Demo restaurant", "purchase", "ILS"),
        (
            "subscription",
            "demo-card",
            "-69.90",
            "Demo streaming",
            "subscription",
            "ILS",
        ),
        ("refund", "demo-card", "20.00", "Demo market", "refund", "ILS"),
        (
            "settlement",
            "demo-bank",
            "-439.90",
            "Demo card payment",
            "card_settlement",
            "ILS",
        ),
        (
            "transfer-out",
            "demo-bank",
            "-1000.00",
            "Demo savings",
            "internal_transfer",
            "ILS",
        ),
        (
            "transfer-in",
            "demo-savings",
            "1000.00",
            "Demo checking",
            "internal_transfer",
            "ILS",
        ),
        ("investment", "demo-bank", "-500.00", "Demo securities", "investment", "ILS"),
        ("income", "demo-bank", "10000.00", "Demo employer", "income", "ILS"),
        ("technical", "demo-bank", "-10.00", "Demo adjustment", "technical", "ILS"),
        ("pending", "demo-card", "-50.00", "Demo pending", "purchase", "ILS"),
        ("reversed", "demo-card", "-90.00", "Demo reversal", "purchase", "ILS"),
        ("foreign", "demo-card", "-10.00", "Demo foreign purchase", "purchase", "USD"),
    ]
    for name, account_id, amount, merchant, kind, currency in examples:
        records.append(
            ProviderTransaction(
                provider_account_id=account_id,
                provider_transaction_id=f"demo-{start:%Y-%m}-{name}",
                transaction_date=start,
                amount=Decimal(amount),
                currency=currency,
                original_description=merchant,
                merchant=merchant,
                transaction_type=kind,
                status=(
                    TransactionStatus.PENDING
                    if name == "pending"
                    else TransactionStatus.REVERSED
                    if name == "reversed"
                    else TransactionStatus.POSTED
                ),
                raw_metadata={"settlement_account_id": "demo-card"}
                if kind == "card_settlement"
                else {},
            )
        )
    return FakeFinanceProvider([bank, card, savings], records)
