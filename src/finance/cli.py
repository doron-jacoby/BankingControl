"""Explicit local output only. Worker logs use their own sanitized allowlist."""

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from finance.analytics import AnalyticsService
from finance.classification import FLAGS, MATCH_TYPES, backfill, save_rule
from finance.demo import demo_provider
from finance.models import Category, ClassificationRule, utc_now
from finance.repository import accounts, transactions
from finance.security import MacOSKeychain, SecretError, load_database_key
from finance.service import FinanceService
from finance.storage import DEFAULT_DATABASE_PATH, StorageError, open_database
from finance.sync import SyncService


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="finance")
    result.add_argument("--demo", action="store_true", help="Use synthetic data only")
    result.add_argument("--data-dir", type=Path, help="Private local data directory")
    result.add_argument("--overlap-days", type=int, default=7)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("sync")
    commands.add_parser("status")
    commands.add_parser("accounts")
    tx = commands.add_parser("transactions")
    tx.add_argument("--days", type=int, default=30)
    monthly = commands.add_parser("monthly")
    monthly.add_argument("month", help="YYYY-MM")
    monthly.add_argument("--timezone", default="Asia/Jerusalem")
    classify = commands.add_parser("classify")
    scope = classify.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true")
    scope.add_argument("--from", dest="from_date", type=datetime.fromisoformat)
    rule = commands.add_parser("rule")
    rule.add_argument("match_type", choices=sorted(MATCH_TYPES))
    rule.add_argument("match_value")
    rule.add_argument("category", choices=[category.value for category in Category])
    rule.add_argument("--priority", type=int, default=0)
    rule.add_argument("--flag", action="append", choices=sorted(FLAGS), default=[])
    return result


def emit(value: Any) -> None:
    print(json.dumps(value, default=str, ensure_ascii=False, indent=2))


def runtime_paths(args: argparse.Namespace) -> tuple[Path, str]:
    directory = args.data_dir or DEFAULT_DATABASE_PATH.parent
    if args.demo:
        return directory / "demo" / "finance.db", "database-key-demo-v1"
    return directory / "finance.db", "database-key-v1"


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    path, key_name = runtime_paths(args)
    if not args.demo:
        emit(
            {
                "status": "not_configured",
                "message": "Live Financy integration awaits its official contract/version. "
                "Run ./install.sh for guided demo setup.",
            }
        )
        return 2
    try:
        if args.command == "status" and not path.exists():
            emit(
                {
                    "provider": "fake",
                    "demo": True,
                    "encrypted_db_available": False,
                    "message": "Run ./install.sh to initialize the demo.",
                }
            )
            return 1
        key = load_database_key(MacOSKeychain(), key_name)
        if args.command == "sync":
            summary = FinanceService(
                SyncService(demo_provider(), path, key, args.overlap_days)
            ).sync()
            emit(asdict(summary))
            return 0 if summary.status in {"completed", "already_running"} else 1
        with open_database(key, path) as db:
            if args.command == "monthly":
                year, month = (int(part) for part in args.month.split("-"))
                emit(
                    asdict(
                        AnalyticsService(db, args.timezone).get_real_monthly_expenses(
                            year, month
                        )
                    )
                )
            elif args.command == "classify":
                since = args.from_date.replace(tzinfo=UTC) if args.from_date else None
                emit({"classified": backfill(db, since)})
            elif args.command == "rule":
                save_rule(
                    db,
                    ClassificationRule(
                        match_type=args.match_type,
                        match_value=args.match_value,
                        category=Category(args.category),
                        priority=args.priority,
                        flags=dict.fromkeys(args.flag, True),
                    ),
                )
                emit(
                    {
                        "status": "saved",
                        "message": "Run finance --demo classify --all to apply.",
                    }
                )
            elif args.command == "accounts":
                emit([asdict(account) for account in accounts(db)])
            elif args.command == "transactions":
                if args.days < 0:
                    raise ValueError("Days must be nonnegative")
                since = utc_now() - timedelta(days=args.days)
                emit(
                    [
                        asdict(tx)
                        for tx in transactions(db)
                        if tx.transaction_date >= since
                    ]
                )
            elif args.command == "status":
                emit(
                    {
                        "provider": "fake",
                        "demo": True,
                        "encrypted_db_available": True,
                        "account_count": db.execute(
                            "SELECT count(*) FROM accounts"
                        ).fetchone()[0],
                        "transaction_count": db.execute(
                            "SELECT count(*) FROM transactions"
                        ).fetchone()[0],
                        "last_successful_sync": db.execute(
                            "SELECT max(last_successful_sync) FROM sync_states"
                        ).fetchone()[0],
                        "last_error": db.execute(
                            "SELECT error FROM sync_states WHERE error IS NOT NULL ORDER BY last_sync_finished_at DESC LIMIT 1"
                        ).fetchone(),
                        "worker_status": "unknown",
                    }
                )
        return 0
    except (SecretError, StorageError, ValueError, OSError):
        emit(
            {
                "status": "failed",
                "error": "local_configuration_or_storage",
                "message": "Check installation, Keychain access and private database permissions.",
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
