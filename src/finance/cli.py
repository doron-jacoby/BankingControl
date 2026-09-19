"""Explicit local output only. Worker logs use their own sanitized allowlist."""

import argparse
import json
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from finance.analytics import AnalyticsService
from finance.classification import FLAGS, MATCH_TYPES, backfill, save_rule
from finance.demo import MonthlyDemoProvider
from finance.exchange import ExchangeRateError, ensure_month_end_rates
from finance.financy import (
    Credentials,
    FinancyClient,
    FinancyError,
    connect_interactively,
)
from finance.live import (
    TAGS,
    LiveLabelRule,
    LiveTagRule,
    PDFExportError,
    card_settlement_matches,
    general_category_trend,
    load_label_rules,
    load_snapshot,
    load_tag_rules,
    save_label_rule,
    save_tag_rule,
    summarize,
    sync_snapshot,
    write_report,
    write_report_pdf,
    write_simple_report,
)
from finance.models import Category, ClassificationRule, utc_now
from finance.repository import accounts, transactions
from finance.security import MacOSKeychain, SecretError, load_database_key
from finance.service import FinanceService
from finance.storage import DEFAULT_DATABASE_PATH, StorageError, open_database
from finance.sync import SyncService
from finance.worker import WorkerSettings, run_demo_worker, worker_status

DEFAULT_REPORT_DIR = Path.home() / "Documents" / "PersonalFinance"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="finance")
    result.add_argument("--demo", action="store_true", help="Use synthetic data only")
    result.add_argument("--data-dir", type=Path, help="Private local data directory")
    result.add_argument("--overlap-days", type=int, default=7)
    commands = result.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync")
    sync.add_argument("--from", dest="start", type=date.fromisoformat)
    sync.add_argument("--to", dest="end", type=date.fromisoformat)
    commands.add_parser("status")
    commands.add_parser("accounts")
    commands.add_parser("connect", help="Save and verify Financy credentials locally")
    tx = commands.add_parser("transactions")
    tx.add_argument("--days", type=int, default=30)
    monthly = commands.add_parser("monthly")
    monthly.add_argument("month", help="YYYY-MM")
    monthly.add_argument("--timezone", default="Asia/Jerusalem")
    report = commands.add_parser("report", help="Write a local provisional live report")
    report.add_argument(
        "--output",
        type=Path,
        help="HTML or PDF path (both are saved); default: ~/Documents/PersonalFinance",
    )
    report.add_argument(
        "--detailed",
        action="store_true",
        help="Export the detailed English diagnostic report",
    )
    report.add_argument(
        "--simple",
        action="store_true",
        help="Short Hebrew monthly spend-by-category trend and flagged charges",
    )
    reconcile = commands.add_parser(
        "reconcile",
        help="Exclude card settlements that exactly equal a card's billed charges",
    )
    reconcile.add_argument(
        "--apply", action="store_true", help="Save the exclusions (default: preview)"
    )
    tag = commands.add_parser(
        "tag", help="Save a manual reconciliation rule for live records"
    )
    tag.add_argument("tag_value", choices=sorted(TAGS))
    tag.add_argument(
        "--account-id", help="Scopes the rule; required alongside --record-id"
    )
    tag.add_argument(
        "--record-id", help="Match one specific record; needs --account-id"
    )
    tag.add_argument("--category", help="Match Financy's category label")
    tag.add_argument("--subcategory", help="Match Financy's subcategory label")
    tag.add_argument("--amount", help="Match Financy's exact signed amount, e.g. -700")
    tag.add_argument(
        "--general-category",
        help="Report category for an expense rule, e.g. פנאי or אחר",
    )
    tag.add_argument(
        "--note", default="", help="Local reminder of why, never sent anywhere"
    )
    tag.add_argument("--priority", type=int, default=0)
    commands.add_parser("tags", help="List saved live reconciliation rules")
    label = commands.add_parser(
        "label", help="Save a display name for records Financy gives no merchant"
    )
    label.add_argument("label_value", help="Display name shown in reports")
    label.add_argument(
        "--account-id", help="Scopes the rule; required alongside --record-id"
    )
    label.add_argument(
        "--record-id", help="Match one specific record; needs --account-id"
    )
    label.add_argument("--category", help="Match Financy's category label")
    label.add_argument("--subcategory", help="Match Financy's subcategory label")
    label.add_argument(
        "--amount", help="Match Financy's exact signed amount, e.g. -700"
    )
    label.add_argument("--priority", type=int, default=0)
    commands.add_parser("labels", help="List saved live display-name rules")
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
    worker = commands.add_parser("worker")
    worker.add_argument(
        "--once", action="store_true", help="Poll and execute once in the foreground"
    )
    worker.add_argument("--poll-seconds", type=int, default=30)
    worker.add_argument("--retry-base-seconds", type=int, default=60)
    worker.add_argument("--max-attempts", type=int, default=3)
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
        return live_command(args, path)
    try:
        if args.command in {"report", "reconcile", "tag", "tags"} or (
            args.command == "sync" and (args.start or args.end)
        ):
            emit(
                {
                    "error": "Report export, tagging and sync date filters are for live data."
                }
            )
            return 2
        if args.command == "connect":
            emit({"error": "Use finance connect without --demo for Financy setup."})
            return 2
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
        if args.command == "worker":
            output = run_demo_worker(
                FinanceService(
                    SyncService(MonthlyDemoProvider(), path, key, args.overlap_days)
                ),
                once=args.once,
                settings=WorkerSettings(
                    poll_seconds=args.poll_seconds,
                    retry_base_seconds=args.retry_base_seconds,
                    max_attempts=args.max_attempts,
                ),
            )
            emit(output)
            return (
                0
                if output["status"] in {"completed", "duplicate", "already_running"}
                else 1
            )
        if args.command == "sync":
            summary = FinanceService(
                SyncService(MonthlyDemoProvider(), path, key, args.overlap_days)
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
                        "worker_status": worker_status(path.parent),
                    }
                )
        return 0
    except KeyboardInterrupt:
        return 130
    except (SecretError, StorageError, ValueError, OSError, KeyError):
        emit(
            {
                "status": "failed",
                "error": "local_configuration_or_storage",
                "message": "Check installation, Keychain access and private database permissions.",
            }
        )
        return 1


def reconcile_command(
    args: argparse.Namespace,
    path: Path,
    store: MacOSKeychain,
    snapshot: dict[str, Any],
    rules: list[LiveTagRule],
) -> int:
    matches = card_settlement_matches(snapshot["records"], rules)
    totals: dict[str, Decimal] = {}
    for settlement, _ in matches:
        currency = settlement["currency"]
        totals[currency] = totals.get(currency, Decimal(0)) - Decimal(
            settlement["amount"]
        )
    if args.apply:
        with open_database(load_database_key(store), path) as db:
            if load_tag_rules(db) != rules:
                emit({"status": "failed", "message": "Rules changed; run it again."})
                return 1
            for settlement, charges in matches:
                save_tag_rule(
                    db,
                    LiveTagRule(
                        tag="self_transfer",
                        account_id=settlement["account_id"],
                        record_id=settlement["id"],
                        priority=100,
                        note=json.dumps(
                            {
                                "reason": "Exact card settlement match",
                                "card_account": charges[0]["account_id"],
                                "charge_day": settlement["day"],
                                "amount": settlement["amount"],
                                "card_charges": len(charges),
                            }
                        ),
                    ),
                )
    emit(
        {
            "status": "applied" if args.apply else "preview",
            "matches": len(matches),
            "totals": {currency: str(total) for currency, total in totals.items()},
            "settlements": [
                {
                    "day": settlement["day"],
                    "amount": settlement["amount"],
                    "currency": settlement["currency"],
                    "card_charges": len(charges),
                }
                for settlement, charges in matches
            ],
            "message": "Excluded from spending."
            if args.apply
            else "Preview only; run with --apply to exclude them.",
        }
    )
    return 0


def live_command(args: argparse.Namespace, path: Path) -> int:
    command = args.command
    if command not in {
        "connect",
        "accounts",
        "status",
        "sync",
        "monthly",
        "report",
        "reconcile",
        "tag",
        "tags",
        "label",
        "labels",
    }:
        emit(
            {
                "status": "contract_incomplete",
                "message": "Use sync, monthly, report, reconcile, tag, tags, label "
                "or labels for "
                "provisional live analysis. Reconciled expense totals and the live "
                "worker remain unavailable.",
            }
        )
        return 2
    try:
        store = MacOSKeychain()
        if command == "connect":
            emit(connect_interactively(store))
        elif command in {
            "monthly",
            "report",
            "reconcile",
            "tag",
            "tags",
            "label",
            "labels",
        }:
            if not path.exists():
                emit({"status": "not_imported", "message": "Run finance sync first."})
                return 1
            if command == "tag":
                try:
                    rule = LiveTagRule(
                        tag=args.tag_value,
                        account_id=args.account_id,
                        record_id=args.record_id,
                        category=args.category,
                        subcategory=args.subcategory,
                        general_category=args.general_category,
                        amount=args.amount,
                        note=args.note,
                        priority=args.priority,
                    )
                    with open_database(load_database_key(store), path) as db:
                        save_tag_rule(db, rule)
                except ValueError as error:
                    emit(
                        {
                            "status": "failed",
                            "error": "validation",
                            "message": str(error),
                        }
                    )
                    return 2
                emit(
                    {
                        "status": "saved",
                        "message": "Run finance monthly or report to apply it.",
                    }
                )
            elif command == "tags":
                with open_database(load_database_key(store), path) as db:
                    emit([asdict(rule) for rule in load_tag_rules(db)])
            elif command == "label":
                try:
                    label_rule = LiveLabelRule(
                        label=args.label_value,
                        account_id=args.account_id,
                        record_id=args.record_id,
                        category=args.category,
                        subcategory=args.subcategory,
                        amount=args.amount,
                        priority=args.priority,
                    )
                    with open_database(load_database_key(store), path) as db:
                        save_label_rule(db, label_rule)
                except ValueError as error:
                    emit(
                        {
                            "status": "failed",
                            "error": "validation",
                            "message": str(error),
                        }
                    )
                    return 2
                emit(
                    {
                        "status": "saved",
                        "message": "Run finance report to apply it.",
                    }
                )
            elif command == "labels":
                with open_database(load_database_key(store), path) as db:
                    emit([asdict(rule) for rule in load_label_rules(db)])
            else:
                snapshot = load_snapshot(path, store)
                with open_database(load_database_key(store), path) as db:
                    rules = load_tag_rules(db)
                    labels = load_label_rules(db)
                if command == "reconcile":
                    return reconcile_command(args, path, store, snapshot, rules)
                if command == "monthly":
                    if args.timezone != "Asia/Jerusalem":
                        emit(
                            {
                                "error": "Live source dates have no time; timezone conversion is unavailable."
                            }
                        )
                        return 2
                    emit(summarize(snapshot, args.month, rules))
                else:
                    output = args.output or DEFAULT_REPORT_DIR / (
                        "analysis.html" if args.detailed else "monthly-overview.html"
                    )
                    output = output.expanduser()
                    if output.suffix.lower() not in {".html", ".pdf"}:
                        raise ValueError("Report output must end in .html or .pdf")
                    output = output.with_suffix(".html")
                    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    if not args.detailed:
                        months = set(general_category_trend(snapshot, rules)["months"])
                        pairs = {
                            (r["currency"], r["day"][:7])
                            for r in snapshot["records"]
                            if r["currency"] != "ILS"
                            and r["amount"] is not None
                            and r["day"][:7] in months
                        }
                        with open_database(load_database_key(store), path) as db:
                            rates = ensure_month_end_rates(db, pairs)
                        write_simple_report(snapshot, output, rules, rates, labels)
                    else:
                        write_report(snapshot, output, rules)
                    pdf = write_report_pdf(output)
                    emit(
                        {
                            "status": "completed",
                            "analysis": "provisional",
                            "report": str(output),
                            "pdf": str(pdf),
                            "card_settlements_to_reconcile": len(
                                card_settlement_matches(snapshot["records"], rules)
                            ),
                        }
                    )
        else:
            client = FinancyClient(Credentials.load(store))
            if command == "accounts":
                emit([asdict(account) for account in client.list_accounts()])
            elif command == "sync":
                end = (
                    args.end or utc_now().astimezone(ZoneInfo("Asia/Jerusalem")).date()
                )
                month_index = end.year * 12 + end.month - 1 - 12
                start = args.start or date(month_index // 12, month_index % 12 + 1, 1)
                output = sync_snapshot(client, path, store, start, end)
                emit(output)
            else:
                emit(
                    {
                        "provider": "financy",
                        "api_access": "verified",
                        "live_import_enabled": True,
                        "analysis": "provisional",
                        "reconciled_expenses_enabled": False,
                        "encrypted_db_available": path.exists(),
                        "snapshot": load_snapshot(path, store)["info"]
                        if path.exists()
                        else None,
                        **client.connection_summary(),
                        "account_count": len(client.list_accounts()),
                    }
                )
        return 0
    except FinancyError as error:
        emit(
            {
                "status": "failed",
                "error": error.code,
                "detail": error.detail,
                "message": "Run finance connect locally. Check Financy Settings -> API, plan and bank connection.",
            }
        )
        return 1
    except PDFExportError as error:
        emit({"status": "failed", "error": "pdf_export", "message": str(error)})
        return 1
    except ExchangeRateError as error:
        emit({"status": "failed", "error": "exchange_rate", "message": str(error)})
        return 1
    except (SecretError, StorageError, OSError, ValueError):
        emit(
            {
                "status": "failed",
                "error": "local_configuration_or_storage",
                "message": "Check date range, snapshot coverage, Keychain and private database permissions.",
            }
        )
        return 1
    except (EOFError, KeyboardInterrupt):
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
