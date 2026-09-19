"""Interactive macOS setup. Demo works now; real consent awaits verified contracts."""

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
    return ask(prompt + " [y/N] ").strip().casefold() in {"y", "yes", "כן"}


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
    print("\nמצב הדגמה בלבד: אין חיבור לבנק או לשרת Conductor. כל הנתונים מומצאים.")
    if not confirm(
        "שלב 1/5 — ליצור מסד מוצפן ומפתח ב־Keychain? macOS עשוי לבקש אישור.", ask
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
    print("ההצפנה והמפתח אומתו. המפתח לא יוצג ולא יישמר בקובץ.")
    provider = demo_provider()
    print(
        f"שלב 2/5 — בדיקת הספק המדומה הצליחה: {len(provider.list_accounts())} חשבונות הדגמה."
    )
    background = confirm(
        "שלב 3/5 — להתקין worker הדגמה קבוע שיריץ סנכרון מדומה פעם בשבוע?", ask
    )
    if background:
        print(
            "ה־worker יפעל כשאתה מחובר למק. הוא לא מעיר מק ישן. הנתונים נשארים מקומיים."
        )
    if not confirm(
        "שלב 4/5 — להריץ כעת פעם אחת את אותו worker, לייבא נתוני הדגמה ולהציג סיכום?",
        ask,
    ):
        print("ההתקנה נעצרה לפני הייבוא. אפשר להריץ שוב את אשף ההתקנה.")
        return 1
    service = FinanceService(SyncService(provider, path, key))
    output = run_demo_worker(service, once=True)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if output.get("status") != "completed":
        print("הרצת הבדיקה לא הושלמה. בדוק את Keychain ואת הרשאות התיקייה והריץ שוב.")
        return 1
    with open_database(key, path) as db:
        now = utc_now()
        summary = AnalyticsService(db).get_real_monthly_expenses(now.year, now.month)
    print("סיכום מקומי של נתוני הדגמה — לא נתוני הבנק שלך:")
    print(json.dumps(asdict(summary), default=str, ensure_ascii=False, indent=2))
    if background:
        destination = (
            launch_agent or Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        )
        write_launch_agent(directory, destination)
        start_launch_agent(destination, directory)
        print("שלב 5/5 — ה־worker הקבוע הופעל ואומתה פעולת polling מדומה.")
    else:
        print("שלב 5/5 — ההדגמה הושלמה. לא הופעל שירות רקע.")
    print(
        "לחיבור אמיתי דרושים עדיין חוזה Financy, הוראות הסכמה רשמיות וגרסת Netflix Conductor."
    )
    return 0


def live_requirements(ask: Callable[[str], str] = input) -> None:
    print("\nהחיבור האמיתי עדיין אינו ממומש. לפני פתיחת חשבון ואישור בבנק צריך להשלים:")
    steps = [
        "1. לקבל מהספק את השם המדויק, גרסת API/SDK וקישור רשמי להרשמה ולתיעוד Financy/Open Finance. "
        "אין כרגע כתובת הרשמה מאומתת בפרויקט.",
        "2. לקבל את תהליך ההסכמה הרשמי לחשבון שלך בבנק לאומי: היכן מאשרים, אילו הרשאות קריאה "
        "מבקשים, תוקף ההסכמה ואיך מבטלים אותה. לא מוסרים לאפליקציה סיסמת בנק.",
        "3. לקבל מספק הנתונים את חוזה החשבונות והעסקאות: מטבעות, סימן סכום, היסטוריה, "
        "pagination, מזהי עסקאות וקישור בין עסקה ממתינה לסופית.",
        "4. לקבל ממנהל Netflix Conductor את הגרסה, ה־SDK המותר, כתובת השרת ושיטת הזדהות "
        "ל־worker. הפרויקט לא יפרוס דבר לשרת המשותף ללא תהליך הפריסה המקובל.",
    ]
    for step in steps:
        print(step)
        ask("Enter להצגת הצעד הבא (אין להקליד סיסמה או token): ")
    print(
        "לא בוצעה הרשמה, בקשת הרשאה או פנייה לבנק. לאחר אימות החוזים נוכל לחבר את השלבים לאשף."
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
            mode = input("מצב התקנה: 1 — הדגמה מלאה; 2 — צעדי חיבור לבנק [1]: ").strip()
            if mode == "2":
                live_requirements()
                if not confirm("להמשיך בינתיים להתקנת ההדגמה?"):
                    return 2
            elif mode not in {"", "1"}:
                return 1
        directory = args.data_dir.expanduser().absolute()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with sync_lock(directory / "install.lock") as acquired:
            if not acquired:
                print("אשף התקנה אחר כבר פועל. נסה שוב לאחר סיומו.")
                return 1
            return setup_demo(directory, MacOSKeychain())
    except (SecretError, StorageError, OSError, ValueError, RuntimeError):
        print(
            "ההתקנה לא הושלמה. בדוק גישה ל־Keychain, הרשאות התיקייה ומצב launchd. "
            "לא הוחלף מפתח קיים; אפשר להריץ את האשף שוב."
        )
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nההתקנה בוטלה. אפשר להמשיך בהפעלה חוזרת של האשף.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
