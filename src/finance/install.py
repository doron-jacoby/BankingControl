"""English guided setup: full demo or verified Financy account discovery."""

import argparse
import os
import plistlib
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from finance.analytics import AnalyticsService
from finance.demo import demo_provider
from finance.financy import FinancyError, connect_interactively
from finance.models import utc_now
from finance.security import (
    SERVICE,
    MacOSKeychain,
    SecretError,
    SecretStore,
    create_database_key,
    load_database_key,
)
from finance.service import FinanceService
from finance.storage import DEFAULT_DATABASE_PATH, StorageError, open_database
from finance.sync import SyncService, sync_lock
from finance.worker import run_demo_worker

LABEL = "com.personalfinance.demo-worker"
DEMO_KEY = "database-key-demo-v1"


def confirm(prompt: str, ask: Callable[[str], str] = input) -> bool:
    return ask(prompt + " [y/N] ").strip().casefold() in {"y", "yes"}


def launchd_definition(python: Path, directory: Path) -> dict[str, object]:
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(python),
            "-m",
            "finance.cli",
            "--demo",
            "--data-dir",
            str(directory),
            "worker",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "Umask": 0o077,
        "WorkingDirectory": str(directory),
        # The app rotates sanitized logs. Never route local financial CLI output
        # into launchd logs, which have no rotation by default.
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }


def write_launch_agent(directory: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as handle:
        plistlib.dump(launchd_definition(Path(sys.executable), directory), handle)


def start_launch_agent(destination: Path, directory: Path) -> None:
    target = f"gui/{os.getuid()}"
    subprocess.run(
        ["/bin/launchctl", "bootout", f"{target}/{LABEL}"],
        capture_output=True,
        check=False,
    )
    started = time.time()
    completed = subprocess.run(
        ["/bin/launchctl", "bootstrap", target, str(destination)],
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError("launchd_start_failed")
    heartbeat = directory / "demo" / "worker-state.json"
    for _ in range(20):
        if heartbeat.exists() and heartbeat.stat().st_mtime >= started:
            status = subprocess.run(
                ["/bin/launchctl", "print", f"{target}/{LABEL}"],
                capture_output=True,
                check=False,
            )
            if status.returncode == 0 and b"state = running" in status.stdout:
                return
        time.sleep(0.5)
    raise RuntimeError("worker_poll_not_verified")


def setup_demo(
    directory: Path,
    store: SecretStore,
    *,
    ask: Callable[[str], str] = input,
    launch_agent: Path | None = None,
) -> int:
    print("\n🧪 Demo — synthetic data only. macOS may ask for Keychain access.")
    if not confirm(
        "Create encrypted storage and run the first demo import?",
        ask,
    ):
        return 1
    path = directory / "demo" / "finance.db"
    if path.exists():
        key = load_database_key(store, DEMO_KEY)
        with open_database(key, path):
            pass
    else:
        key = (
            load_database_key(store, DEMO_KEY)
            if store.get_password(SERVICE, DEMO_KEY) is not None
            else create_database_key(store, DEMO_KEY)
        )
        with open_database(key, path, create=True):
            pass
    print("🔒 Encrypted storage ready.")
    provider = demo_provider()
    print("⏳ Running the demo worker once…")
    service = FinanceService(SyncService(provider, path, key))
    output = run_demo_worker(service, once=True)
    if output.get("status") != "completed":
        print("⚠ Demo run failed. Check Keychain and folder permissions, then retry.")
        return 1
    with open_database(key, path) as db:
        now = utc_now()
        summary = AnalyticsService(db).get_real_monthly_expenses(now.year, now.month)
    print(f"✓ Demo import complete: {output['accounts_processed']} accounts.")
    print(f"📊 Demo expenses — {summary.year}-{summary.month:02d}:")
    for currency, totals in sorted(summary.currencies.items()):
        print(f"   {currency} {totals.total:.2f}")
    if confirm("Run weekly demo syncs in the background while logged in?", ask):
        destination = (
            launch_agent or Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        )
        write_launch_agent(directory, destination)
        start_launch_agent(destination, directory)
        print("✓ Background demo worker running. Syncs resume when your Mac wakes.")
    else:
        print("✓ Demo setup complete.")
    return 0


def live_requirements() -> None:
    print(
        "\n🌐 Sign in: https://financy.open-finance.ai\n"
        "🏦 Add your bank in Financy and complete its permission screens.\n"
        "This connects your accounts. Live transaction import is not enabled yet."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Personal Finance guided setup")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATABASE_PATH.parent)
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args(argv)
    if sys.platform != "darwin" or sys.version_info < (3, 12):
        print("macOS and Python 3.12+ are required.")
        return 1
    try:
        if not args.demo:
            mode = input("\n1 🧪 Demo\n2 🏦 Connect Financy\nChoose [1]: ").strip()
            if mode == "2":
                live_requirements()
                summary = connect_interactively(MacOSKeychain())
                print("🔒 Credentials verified and saved in Keychain.")
                print(f"✓ Found {summary['account_count']} accounts.")
                if summary["connections_requiring_attention"]:
                    print(
                        "⚠ Open Financy to renew bank permissions or fix its connection alerts."
                    )
                elif summary["account_count"] == 0:
                    print("🏦 Add your bank in Financy, then rerun setup.")
                print("✓ Financy setup complete.")
                return 0
            elif mode not in {"", "1"}:
                return 1
        directory = args.data_dir.expanduser().absolute()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with sync_lock(directory / "install.lock") as acquired:
            if not acquired:
                print("Another installer is running. Retry after it finishes.")
                return 1
            return setup_demo(directory, MacOSKeychain())
    except FinancyError as error:
        guidance = {
            "authentication": "Credentials rejected. Copy all three values again from Settings -> API.",
            "plan": "API access is unavailable on your Financy plan. Check your plan in Financy.",
            "forbidden": "Access denied. Check API permissions in Financy.",
            "transient": "Financy is temporarily unavailable. Check your connection and retry.",
        }.get(
            error.code,
            "Financy verification failed. Share the diagnostic below to investigate.",
        )
        print(f"⚠ {guidance}")
        if error.detail:
            print(f"   {error.detail}")
        return 1
    except (SecretError, StorageError, OSError, ValueError, RuntimeError):
        print(
            "Setup did not complete. Check Keychain access, directory permissions and launchd. "
            "No existing database key was replaced. You can rerun the wizard."
        )
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled. Rerun the wizard to continue.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
