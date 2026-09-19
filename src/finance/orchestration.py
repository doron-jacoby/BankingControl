"""Thin local adapter contract and fakes, not a Netflix Conductor SDK contract."""

import hashlib
import re
from collections import deque
from dataclasses import dataclass
from typing import Protocol

from finance.models import utc_now
from finance.service import FinanceService
from finance.storage import open_database
from finance.sync import SyncResult, sync_lock

type TaskOutput = dict[str, str | int | bool]


@dataclass(frozen=True, repr=False)
class SyncTask:
    request_id: str
    mode: str = "incremental"


class ConductorAdapter(Protocol):
    """Our internal port. Implement against a verified SDK in a later change."""

    def poll(self) -> SyncTask | None: ...
    def complete(self, task: SyncTask, output: TaskOutput) -> None: ...


class FakeConductorAdapter:
    def __init__(self, tasks: list[SyncTask]) -> None:
        self._tasks = deque(tasks)
        self.outputs: list[TaskOutput] = []

    def poll(self) -> SyncTask | None:
        return self._tasks.popleft() if self._tasks else None

    def complete(self, task: SyncTask, output: TaskOutput) -> None:
        self.outputs.append(dict(output))


def safe_output(result: SyncResult) -> TaskOutput:
    """Explicit allowlist; never serialize the service or arbitrary exception."""
    output: TaskOutput = {
        "status": result.status,
        "accounts_processed": result.accounts_processed,
        "inserted": result.inserted,
        "updated": result.updated,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
    }
    if result.errors:
        # Mixed failures must not retry authentication or validation indefinitely.
        code = next(
            (error for error in result.errors if error != "transient"), "transient"
        )
        output["error"] = (
            code
            if code
            in {
                "transient",
                "authentication",
                "validation",
                "storage",
                "classification",
            }
            else "validation"
        )
        output["retryable"] = all(error == "transient" for error in result.errors)
    return output


def _empty(status: str) -> TaskOutput:
    now = utc_now().isoformat()
    return {
        "status": status,
        "accounts_processed": 0,
        "inserted": 0,
        "updated": 0,
        "started_at": now,
        "finished_at": now,
    }


def execute_task(service: FinanceService, task: SyncTask) -> TaskOutput:
    if not re.fullmatch(
        r"[A-Za-z0-9._:-]{1,128}", task.request_id
    ) or task.mode not in {"incremental", "bootstrap"}:
        return _empty("failed") | {"error": "validation", "retryable": False}
    request_hash = hashlib.sha256(task.request_id.encode()).hexdigest()
    sync = service.sync_service
    try:
        # This lock covers receipt lookup through completion; the source sync lock
        # also excludes a concurrently running CLI import.
        with sync_lock(sync.path.with_suffix(".worker.lock")) as acquired:
            if not acquired:
                return _empty("already_running")
            with open_database(sync.key, sync.path) as db:
                if db.execute(
                    "SELECT 1 FROM task_receipts WHERE request_hash=?", (request_hash,)
                ).fetchone():
                    return _empty("duplicate")
            output = safe_output(service.sync())
            if output["status"] == "completed":
                with open_database(sync.key, sync.path) as db:
                    db.execute(
                        "INSERT INTO task_receipts VALUES (?, ?)",
                        (request_hash, utc_now().isoformat()),
                    )
            return output
    except Exception:
        # Never forward a third-party exception message or traceback to Conductor.
        return _empty("failed") | {"error": "local_failure", "retryable": False}


def run_once(service: FinanceService, adapter: ConductorAdapter) -> TaskOutput | None:
    task = adapter.poll()
    if task is None:
        return None
    output = execute_task(service, task)
    adapter.complete(task, output)
    return output
