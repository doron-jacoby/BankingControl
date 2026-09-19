"""Recomputable classification with manual rules ahead of deterministic defaults."""

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Protocol

from sqlcipher3 import dbapi2 as sqlcipher

from finance.models import Category, ClassificationRule, Transaction, utc_now
from finance.repository import dictionaries, save_record, transactions

VERSION = 1
FLAGS = {
    "is_internal_transfer",
    "is_investment",
    "is_income",
    "is_refund",
    "is_recurring",
}
MATCH_TYPES = {"merchant_exact", "description_contains", "transaction_type"}


@dataclass(frozen=True)
class Classification:
    category: Category = Category.OTHER
    category_source: str = "default"
    classification_version: int = VERSION
    is_internal_transfer: bool = False
    is_investment: bool = False
    is_income: bool = False
    is_refund: bool = False
    is_recurring: bool = False


class TransactionClassifier(Protocol):
    def classify(self, transaction: Transaction) -> Classification: ...


def save_rule(db: sqlcipher.Connection, rule: ClassificationRule) -> None:
    if rule.match_type not in MATCH_TYPES or not rule.match_value.strip():
        raise ValueError("Unsupported or empty manual rule")
    if any(
        name not in FLAGS or not isinstance(value, bool)
        for name, value in rule.flags.items()
    ):
        raise ValueError("Unsupported classification flag")
    db.execute(
        "INSERT INTO classification_rules VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(internal_id) DO UPDATE SET match_type=excluded.match_type, "
        "match_value=excluded.match_value, category=excluded.category, flags=excluded.flags, "
        "priority=excluded.priority, enabled=excluded.enabled, updated_at=excluded.updated_at",
        (
            rule.internal_id,
            rule.match_type,
            rule.match_value,
            rule.category,
            json.dumps(rule.flags),
            rule.priority,
            rule.enabled,
            rule.created_at.isoformat(),
            rule.updated_at.isoformat(),
        ),
    )


def load_rules(db: sqlcipher.Connection) -> list[ClassificationRule]:
    result = []
    for row in dictionaries(
        db,
        "SELECT * FROM classification_rules WHERE enabled=1 ORDER BY priority DESC, internal_id",
    ):
        row["category"] = Category(row["category"])
        row["flags"] = json.loads(row["flags"])
        for name in ("created_at", "updated_at"):
            row[name] = datetime.fromisoformat(row[name])
        result.append(ClassificationRule(**row))
    return result


class RuleClassifier:
    def __init__(self, rules: list[ClassificationRule]) -> None:
        self.rules = sorted(
            (rule for rule in rules if rule.enabled),
            key=lambda r: (-r.priority, r.internal_id),
        )

    def classify(self, transaction: Transaction) -> Classification:
        automatic = self._automatic(transaction)
        merchant = (transaction.merchant or "").strip().casefold()
        description = transaction.original_description.casefold()
        for rule in self.rules:
            value = rule.match_value.strip().casefold()
            matched = (
                rule.match_type == "merchant_exact"
                and merchant == value
                or rule.match_type == "description_contains"
                and value in description
                or rule.match_type == "transaction_type"
                and transaction.transaction_type.casefold() == value
            )
            if matched:
                return replace(
                    automatic,
                    category=rule.category,
                    category_source="manual",
                    **rule.flags,
                )
        return automatic

    @staticmethod
    def _automatic(transaction: Transaction) -> Classification:
        merchant = (transaction.merchant or "").strip().casefold()
        known = {
            "demo market": Category.FOOD,
            "demo restaurant": Category.RESTAURANT,
            "demo streaming": Category.SUBSCRIPTIONS,
        }
        # Source movement types still determine safety flags for known merchants.
        kind = transaction.transaction_type
        flags = {
            "is_internal_transfer": kind == "internal_transfer",
            "is_investment": kind
            in {"investment", "securities_purchase", "transfer_to_investment"},
            "is_income": kind == "income",
            "is_refund": kind in {"refund", "reversal"},
            "is_recurring": kind == "subscription",
        }
        category = known.get(merchant)
        if category is not None:
            return Classification(
                category=category, category_source="merchant", **flags
            )
        categories = {
            "internal_transfer": Category.TRANSFER,
            "card_settlement": Category.TRANSFER,
            "investment": Category.INVESTMENT,
            "securities_purchase": Category.INVESTMENT,
            "transfer_to_investment": Category.INVESTMENT,
            "income": Category.INCOME,
            "subscription": Category.SUBSCRIPTIONS,
            "cash_withdrawal": Category.CASH,
        }
        if kind in categories:
            return Classification(
                category=categories[kind], category_source="heuristic", **flags
            )
        return Classification(
            category=Category.OTHER, category_source="default", **flags
        )


def backfill(db: sqlcipher.Connection, since: datetime | None = None) -> int:
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    classifier: TransactionClassifier = RuleClassifier(load_rules(db))
    count = 0
    for transaction in transactions(db):
        if since is not None and transaction.transaction_date < since:
            continue
        result = classifier.classify(transaction)
        candidate = replace(
            transaction,
            normalized_merchant=(transaction.merchant or "").strip().casefold() or None,
            **asdict(result),
        )
        if candidate != transaction:
            save_record(db, replace(candidate, updated_at=utc_now()))
            count += 1
    return count
