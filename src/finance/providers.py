"""Read-only contract and deterministic fake; no guessed Financy endpoints."""

from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

from finance.models import (
    Account,
    ProviderTransaction,
    TransactionStatus,
    require_aware,
)


class ProviderError(RuntimeError):
    """Safe provider error codes; adapters must discard raw response messages."""

    def __init__(self, code: str = "transient") -> None:
        if code not in {"transient", "authentication", "validation"}:
            code = "validation"
        self.code = code
        super().__init__(code)


def normalize_demo_record(payload: dict[str, object]) -> ProviderTransaction:
    """Our synthetic fixture format, NOT a claimed Financy payload contract.

    No whole response is retained. Only the allowlisted pending link is copied.
    Real provider normalization awaits its published contract and sign semantics.
    """
    try:
        amount = payload["amount"]
        timestamp = payload["date"]
        account_id = payload["account_id"]
        currency = payload["currency"]
        description = payload["description"]
        transaction_id = payload.get("id")
        merchant = payload.get("merchant")
        pending_id = payload.get("pending_id")
        status = payload.get("status", "posted")
        if not all(
            isinstance(value, str)
            for value in (amount, timestamp, account_id, currency, description)
        ):
            raise ValueError
        if any(
            value is not None and not isinstance(value, str)
            for value in (transaction_id, merchant, pending_id)
        ):
            raise ValueError
        # Individual checks give the type checker the narrowed types as well.
        assert isinstance(amount, str) and isinstance(timestamp, str)
        assert isinstance(account_id, str) and isinstance(currency, str)
        assert isinstance(description, str)
        assert transaction_id is None or isinstance(transaction_id, str)
        assert merchant is None or isinstance(merchant, str)
        assert pending_id is None or isinstance(pending_id, str)
        if not isinstance(status, str):
            raise ValueError
        return ProviderTransaction(
            provider_account_id=account_id,
            provider_transaction_id=transaction_id,
            transaction_date=datetime.fromisoformat(timestamp),
            amount=Decimal(amount),
            currency=currency,
            original_description=description,
            merchant=merchant,
            status=TransactionStatus(status),
            raw_metadata={"pending_transaction_id": pending_id} if pending_id else {},
        )
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise ProviderError("validation") from None


class FinanceProvider(Protocol):
    def list_accounts(self) -> list[Account]: ...

    def fetch_transactions(
        self, account_id: str, from_date: datetime, to_date: datetime
    ) -> list[ProviderTransaction]:
        """Return normalized records within the inclusive timestamp range."""
        ...


class FakeFinanceProvider:
    """Caller-supplied synthetic records only; never accesses a network."""

    def __init__(
        self,
        accounts: list[Account],
        transactions: list[ProviderTransaction],
    ) -> None:
        account_ids = {account.provider_account_id for account in accounts}
        if len(account_ids) != len(accounts):
            raise ValueError("Duplicate fake provider account IDs")
        if len({account.provider for account in accounts}) > 1:
            raise ValueError("A provider adapter represents one provider")
        if any(tx.provider_account_id not in account_ids for tx in transactions):
            raise ValueError("Fake transaction references an unknown account")
        self._accounts = deepcopy(accounts)
        self._transactions = deepcopy(transactions)

    def list_accounts(self) -> list[Account]:
        return deepcopy(self._accounts)

    def fetch_transactions(
        self, account_id: str, from_date: datetime, to_date: datetime
    ) -> list[ProviderTransaction]:
        require_aware(from_date)
        require_aware(to_date)
        if from_date > to_date:
            raise ValueError("Date range must be ordered")
        if not any(a.provider_account_id == account_id for a in self._accounts):
            raise ValueError("Unknown fake provider account")
        return deepcopy(
            [
                tx
                for tx in self._transactions
                if tx.provider_account_id == account_id
                and from_date <= tx.transaction_date <= to_date
            ]
        )
