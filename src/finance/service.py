"""Application entry point independent of any provider or orchestrator SDK."""

from datetime import datetime

from finance.classification import backfill
from finance.storage import StorageError, open_database
from finance.sync import SyncResult, SyncService


class FinanceService:
    def __init__(self, sync_service: SyncService) -> None:
        self.sync_service = sync_service

    def sync(self, *, now: datetime | None = None) -> SyncResult:
        result = self.sync_service.sync(now=now)
        if result.status == "already_running":
            return result
        # Classification has its own transaction after source imports commit.
        try:
            with open_database(self.sync_service.key, self.sync_service.path) as db:
                backfill(db)
        except (StorageError, ValueError, TypeError):
            result.errors.append("classification")
            result.status = "partial"
        return result
