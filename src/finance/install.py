"""English guided setup: full demo or verified Financy account discovery."""

import argparse
import json
import os
import plistlib
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
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
    print("\nDemo mode: no bank or Conductor server connection. All data is synthetic.")
    if not confirm(
        "Step 1/5 - Create an encrypted database and Keychain key? macOS may ask for permission.",
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
    print("Encryption and key verified. The key is never displayed or saved to a file.")
    provider = demo_provider()
    print(
        f"Step 2/5 - Fake provider verified: {len(provider.list_accounts())} demo accounts."
    )
    background = confirm(
        "Step 3/5 - Install a persistent demo worker for weekly synthetic syncs?", ask
    )
    if background:
        print(
            "The worker runs while you are logged in. It cannot wake a sleeping Mac. Data stays local."
        )
    if not confirm(
        "Step 4/5 - Run the same worker once now, import demo data and display a summary?",
        ask,
    ):
        print("Setup stopped before importing. Run the wizard again to continue.")
        return 1
    service = FinanceService(SyncService(provider, path, key))
    output = run_demo_worker(service, once=True)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if output.get("status") != "completed":
        print(
            "The test run did not complete. Check Keychain and directory permissions, then retry."
        )
        return 1
    with open_database(key, path) as db:
        now = utc_now()
        summary = AnalyticsService(db).get_real_monthly_expenses(now.year, now.month)
    print("Local summary of synthetic demo data - not your bank data:")
    print(json.dumps(asdict(summary), default=str, ensure_ascii=False, indent=2))
    if background:
        destination = (
            launch_agent or Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        )
        write_launch_agent(directory, destination)
        start_launch_agent(destination, directory)
        print("Step 5/5 - Background worker started and fake polling verified.")
    else:
        print("Step 5/5 - Demo complete. No background service was started.")
    print(
        "Live account discovery is available in Financy setup. Live transaction import and Conductor automation are not enabled yet."
    )
    return 0


def live_requirements(ask: Callable[[str], str] = input) -> None:
    print(
        "\nFinancy setup: verified authentication and account discovery are available."
    )
    steps = [
        "1. Open https://financy.open-finance.ai and sign in to your existing account. "
        "Data API access requires an eligible paid plan; check your plan in Financy.",
        "2. In Financy, add Bank Leumi (or your bank/card) and follow the bank's hosted "
        "consent screens. Complete the permissions there and return to Financy. "
        "Do not enter your bank password into this installer.",
        "3. Open Settings -> API in Financy and locate the actual clientId, clientSecret "
        "and userId values. An API availability badge only confirms your plan includes access. "
        "If those fields are missing, stop here and ask Financy support where to find them. "
        "The next step reads these values with hidden input and stores them in macOS Keychain.",
        "4. This setup verifies credentials and discovers accounts only. Live transaction import "
        "is not enabled: documented status values and pending/final links still need confirmation. "
        "Production Netflix Conductor integration also awaits its deployed version and SDK.",
    ]
    for step in steps:
        print(step)
        ask(
            "Press Enter when ready for the next step (do not type a password or token here): "
        )
    print("Official guide: https://docs-financy.open-finance.ai/docs/authentication")


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
            mode = input(
                "Setup mode: 1 - Full demo; 2 - Connect Financy accounts [1]: "
            ).strip()
            if mode == "2":
                live_requirements()
                if not confirm(
                    "Verify Financy credentials and save them in macOS Keychain?"
                ):
                    return 1
                print(json.dumps(connect_interactively(MacOSKeychain()), indent=2))
                print(
                    "Account discovery setup complete. Run finance accounts to view accounts locally."
                )
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
        print(
            f"Financy verification failed: {error.code}. Check Settings -> API and your plan or connection."
        )
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
