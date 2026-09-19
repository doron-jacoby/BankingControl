"""A local weekly fake for development; no external server or guessed SDK calls."""

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from finance.models import utc_now
from finance.orchestration import FakeConductorAdapter, SyncTask, TaskOutput, run_once
from finance.service import FinanceService


@dataclass(frozen=True)
class WorkerSettings:
    poll_seconds: int = 30
    retry_base_seconds: int = 60
    max_attempts: int = 3
    bootstrap_timeout_seconds: int = 14400
    response_timeout_seconds: int = 18000

    def __post_init__(self) -> None:
        if (
            min(
                self.poll_seconds,
                self.retry_base_seconds,
                self.max_attempts,
                self.bootstrap_timeout_seconds,
                self.response_timeout_seconds,
            )
            < 1
        ):
            raise ValueError("Worker settings must be positive")

    def retry_delay(self, attempt: int) -> int:
        return min(3600, self.retry_base_seconds * (1 << min(max(0, attempt - 1), 12)))


def worker_logger(directory: Path) -> logging.Logger:
    os.umask(0o077)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError("Worker log directory must be private")
    log_path = directory / "worker.log"
    descriptor = os.open(
        log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600
    )
    os.close(descriptor)
    if log_path.stat().st_mode & 0o077:
        raise ValueError("Worker log must be private")
    logger = logging.getLogger("finance.worker")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
    handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


def write_heartbeat(path: Path) -> None:
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(
        temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as handle:
        json.dump({"last_poll": utc_now().isoformat(), "adapter": "fake"}, handle)
    temporary.replace(path)


def worker_status(directory: Path) -> str:
    try:
        data = json.loads((directory / "worker-state.json").read_text())
        age = (utc_now() - datetime.fromisoformat(data["last_poll"])).total_seconds()
        return "recent_poll" if 0 <= age <= 120 else "stale_poll"
    except (OSError, ValueError, KeyError, TypeError):
        return "not_observed"


def log_result(
    logger: logging.Logger, request_id: str, output: TaskOutput, duration: float
) -> None:
    fields: TaskOutput = {
        key: output[key]
        for key in ("status", "accounts_processed", "inserted", "updated", "error")
        if key in output
    }
    fields.update(
        timestamp=utc_now().isoformat(),
        provider="fake",
        execution_id=hashlib.sha256(request_id.encode()).hexdigest()[:16],
        duration_seconds=f"{duration:.3f}",
    )
    logger.info(json.dumps(fields))


def run_demo_worker(
    service: FinanceService,
    *,
    once: bool = False,
    settings: WorkerSettings | None = None,
) -> TaskOutput:
    """Weekly fake requests survive restart via encrypted task receipts.

    The production Conductor client must enforce the configured task/response
    timeouts. This local fake has no remote task lease to time out.
    """
    settings = settings or WorkerSettings()
    logger = worker_logger(service.sync_service.path.parent / "logs")
    last_request = ""
    attempt = 0
    retry_at = 0.0
    output: TaskOutput = {"status": "idle"}
    while True:
        now = utc_now()
        write_heartbeat(service.sync_service.path.parent / "worker-state.json")
        iso_year, week, _ = now.isocalendar()
        request_id = f"demo-week-{iso_year}-{week:02d}"
        if once:
            request_id = f"demo-check-{now:%Y%m%dT%H%M%S%fZ}"
        if request_id != last_request or retry_at and time.monotonic() >= retry_at:
            if request_id != last_request:
                attempt = 0
            last_request = request_id
            attempt += 1
            started = time.monotonic()
            adapter = FakeConductorAdapter([SyncTask(request_id)])
            output = run_once(service, adapter) or {"status": "idle"}
            log_result(logger, request_id, output, time.monotonic() - started)
            retry_at = (
                time.monotonic() + settings.retry_delay(attempt)
                if output.get("retryable") is True and attempt < settings.max_attempts
                else 0.0
            )
        if once:
            return output
        time.sleep(settings.poll_seconds)
