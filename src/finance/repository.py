"""Small SQL helpers; no provider-specific behavior or financial calculations."""

import json
from dataclasses import asdict, replace
from datetime import date, datetime
from typing import Any

from sqlcipher3 import dbapi2 as sqlcipher

from finance.models import Account, Category, Transaction, TransactionStatus


def parameters(record: Account | Transaction) -> dict[str, Any]:
    result = asdict(record)
    for name, value in result.items():
        if isinstance(value, datetime | date):
            result[name] = value.isoformat()
        elif isinstance(value, dict):
            result[name] = json.dumps(value, sort_keys=True, allow_nan=False)
    return result


def save_record(db: sqlcipher.Connection, record: Account | Transaction) -> None:
    table = "accounts" if isinstance(record, Account) else "transactions"
    values = parameters(record)
    columns = list(values)
    # Identifiers come exclusively from the application's dataclass definitions.
    db.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)}) "
        "ON CONFLICT(internal_id) DO UPDATE SET "
        + ", ".join(f"{column}=excluded.{column}" for column in columns),
        tuple(values.values()),
    )


def dictionaries(
    db: sqlcipher.Connection, sql: str, values: tuple[object, ...] = ()
) -> list[dict[str, Any]]:
    cursor = db.execute(sql, values)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def accounts(db: sqlcipher.Connection) -> list[Account]:
    result = []
    for row in dictionaries(db, "SELECT * FROM accounts ORDER BY internal_id"):
        for field in ("created_at", "updated_at"):
            row[field] = datetime.fromisoformat(row[field])
        row["metadata"] = json.loads(row["metadata"])
        result.append(Account(**row))
    return result


def transaction_from_row(row: dict[str, Any]) -> Transaction:
    row = dict(row)
    for field in ("transaction_date", "created_at", "updated_at"):
        row[field] = datetime.fromisoformat(row[field])
    if row["value_date"] is not None:
        row["value_date"] = date.fromisoformat(row["value_date"])
    row["raw_metadata"] = json.loads(row["raw_metadata"])
    row["status"] = TransactionStatus(row["status"])
    row["category"] = Category(row["category"])
    for field in (
        "is_internal_transfer",
        "is_investment",
        "is_income",
        "is_refund",
        "is_recurring",
    ):
        row[field] = bool(row[field])
    return Transaction(**row)


def transactions(db: sqlcipher.Connection) -> list[Transaction]:
    return [
        transaction_from_row(row)
        for row in dictionaries(
            db, "SELECT * FROM transactions ORDER BY transaction_date"
        )
    ]


def save_account(db: sqlcipher.Connection, incoming: Account) -> Account:
    existing = db.execute(
        "SELECT internal_id, created_at FROM accounts "
        "WHERE provider = ? AND provider_account_id = ?",
        (incoming.provider, incoming.provider_account_id),
    ).fetchone()
    if existing:
        incoming = replace(
            incoming,
            internal_id=existing[0],
            created_at=datetime.fromisoformat(existing[1]),
        )
    save_record(db, incoming)
    return incoming
