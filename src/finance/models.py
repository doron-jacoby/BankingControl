"""Provider-neutral models. Amounts are signed: debits negative, credits positive.

Metadata must contain only necessary, sanitized provider attributes, never secrets
or entire provider response bodies. Real adapters must enforce their allowlist.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

LOCAL_USER_ID = "local"
type Metadata = dict[str, str | int | bool | None]


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


def require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")


def validate_currency(value: str) -> None:
    if not re.fullmatch(r"[A-Z]{3}", value):
        raise ValueError("Currency must be a three-letter uppercase code")


def validate_money(amount: Decimal, currency: str) -> None:
    if not isinstance(amount, Decimal) or not amount.is_finite():
        raise ValueError("Amount must be a finite Decimal")
    validate_currency(currency)


class TransactionStatus(StrEnum):
    PENDING = "pending"
    POSTED = "posted"
    REVERSED = "reversed"


class Category(StrEnum):
    FOOD = "FOOD"
    RESTAURANT = "RESTAURANT"
    ENTERTAINMENT = "ENTERTAINMENT"
    SHOPPING = "SHOPPING"
    TRANSPORT = "TRANSPORT"
    TRAVEL = "TRAVEL"
    UTILITIES = "UTILITIES"
    HEALTH = "HEALTH"
    EDUCATION = "EDUCATION"
    SUBSCRIPTIONS = "SUBSCRIPTIONS"
    INSURANCE = "INSURANCE"
    TAX = "TAX"
    INCOME = "INCOME"
    TRANSFER = "TRANSFER"
    INVESTMENT = "INVESTMENT"
    CASH = "CASH"
    OTHER = "OTHER"


class SyncStatus(StrEnum):
    NEVER = "never"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SyncError(StrEnum):
    """Only these codes may be persisted; never exception messages."""

    TRANSIENT = "transient"
    AUTHENTICATION = "authentication"
    VALIDATION = "validation"
    STORAGE = "storage"


@dataclass(frozen=True, kw_only=True, repr=False)
class Account:
    provider: str
    provider_account_id: str
    institution: str
    account_type: str
    display_name: str
    currency: str
    internal_id: str = field(default_factory=new_id)
    user_id: str = LOCAL_USER_ID
    connection_id: str | None = None
    metadata: Metadata = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_currency(self.currency)
        if not self.provider or not self.provider_account_id:
            raise ValueError("Provider and provider account ID are required")
        if self.user_id != LOCAL_USER_ID:
            raise ValueError("Only the local user is supported")
        require_aware(self.created_at)
        require_aware(self.updated_at)


@dataclass(frozen=True, kw_only=True, repr=False)
class ProviderTransaction:
    """Normalized provider record; its account ID belongs to the provider."""

    provider_account_id: str
    provider_transaction_id: str | None
    transaction_date: datetime
    amount: Decimal
    currency: str
    original_description: str
    merchant: str | None = None
    value_date: date | None = None
    status: TransactionStatus = TransactionStatus.POSTED
    transaction_type: str = "unknown"
    raw_metadata: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_money(self.amount, self.currency)
        require_aware(self.transaction_date)
        if not self.provider_account_id:
            raise ValueError("Provider account ID is required")
        if self.provider_transaction_id == "":
            raise ValueError("Missing transaction IDs must use None")
        if not isinstance(self.status, TransactionStatus):
            raise ValueError("Unsupported transaction status")


@dataclass(frozen=True, kw_only=True, repr=False)
class Transaction:
    account_id: str
    provider: str
    provider_transaction_id: str | None
    transaction_date: datetime
    amount: Decimal
    currency: str
    original_description: str
    internal_id: str = field(default_factory=new_id)
    user_id: str = LOCAL_USER_ID
    value_date: date | None = None
    merchant: str | None = None
    normalized_merchant: str | None = None
    status: TransactionStatus = TransactionStatus.POSTED
    transaction_type: str = "unknown"
    category: Category = Category.OTHER
    category_source: str = "unclassified"
    classification_version: int = 0
    is_internal_transfer: bool = False
    is_investment: bool = False
    is_income: bool = False
    is_refund: bool = False
    is_recurring: bool = False
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    raw_metadata: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_money(self.amount, self.currency)
        for timestamp in (self.transaction_date, self.created_at, self.updated_at):
            require_aware(timestamp)
        if self.user_id != LOCAL_USER_ID:
            raise ValueError("Only the local user is supported")
        if not self.account_id or not self.provider:
            raise ValueError("Account ID and provider are required")
        if self.provider_transaction_id == "":
            raise ValueError("Missing transaction IDs must use None")
        if not isinstance(self.status, TransactionStatus):
            raise ValueError("Unsupported transaction status")
        if not isinstance(self.category, Category):
            raise ValueError("Unsupported category")


@dataclass(frozen=True, kw_only=True, repr=False)
class SyncState:
    provider: str
    account_id: str
    last_successful_sync: datetime | None = None
    last_transaction_date: datetime | None = None
    last_sync_started_at: datetime | None = None
    last_sync_finished_at: datetime | None = None
    status: SyncStatus = SyncStatus.NEVER
    error: SyncError | None = None
    bootstrap_complete: bool = False

    def __post_init__(self) -> None:
        for timestamp in (
            self.last_successful_sync,
            self.last_transaction_date,
            self.last_sync_started_at,
            self.last_sync_finished_at,
        ):
            if timestamp is not None:
                require_aware(timestamp)
        if not isinstance(self.status, SyncStatus):
            raise ValueError("Unsupported sync status")
        if self.error is not None and not isinstance(self.error, SyncError):
            raise ValueError("Sync errors must use sanitized codes")


@dataclass(frozen=True, kw_only=True, repr=False)
class ClassificationRule:
    match_type: str
    match_value: str
    category: Category
    internal_id: str = field(default_factory=new_id)
    flags: dict[str, bool] = field(default_factory=dict)
    priority: int = 0
    enabled: bool = True
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        require_aware(self.created_at)
        require_aware(self.updated_at)
        if not isinstance(self.category, Category):
            raise ValueError("Unsupported category")
