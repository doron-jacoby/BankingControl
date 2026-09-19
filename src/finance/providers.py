"""Read-only contract and deterministic fake; no guessed Financy endpoints."""

from copy import deepcopy
from datetime import datetime
from typing import Protocol

from finance.models import Account, ProviderTransaction, require_aware


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
