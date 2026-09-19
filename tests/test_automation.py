import contextlib
import io
import json
import logging
import plistlib
import secrets
import tempfile
import unittest
from itertools import count
from pathlib import Path
from unittest.mock import Mock, patch

from test_foundation import NOW

from finance.demo import MonthlyDemoProvider, demo_provider
from finance.install import launchd_definition, setup_demo
from finance.install import main as install_main
from finance.orchestration import FakeConductorAdapter, SyncTask, execute_task, run_once
from finance.security import FakeSecretStore
from finance.service import FinanceService
from finance.storage import open_database
from finance.sync import SyncResult, SyncService, sync_lock
from finance.worker import WorkerSettings, log_result, run_demo_worker


class AutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "finance.db"
        self.key = secrets.token_hex(32)
        with open_database(self.key, self.path, create=True):
            pass
        self.service = FinanceService(SyncService(demo_provider(), self.path, self.key))

    def test_fake_poll_and_duplicate_delivery_after_restart(self) -> None:
        task = SyncTask("fake-request-1")
        adapter = FakeConductorAdapter([task, task])
        output = run_once(self.service, adapter)
        self.assertIsNotNone(output)
        self.assertEqual(adapter.outputs[0]["inserted"], 13)
        restarted = FinanceService(SyncService(demo_provider(), self.path, self.key))
        output = run_once(restarted, adapter)
        self.assertIsNotNone(output)
        self.assertEqual(adapter.outputs[1]["status"], "duplicate")
        self.assertEqual(adapter.outputs[1]["inserted"], 0)
        self.assertIsNone(run_once(restarted, adapter))

    def test_no_sensitive_fields_or_values_in_task_output(self) -> None:
        output = execute_task(self.service, SyncTask("secret-looking-request"))
        self.assertEqual(
            set(output),
            {
                "status",
                "accounts_processed",
                "inserted",
                "updated",
                "started_at",
                "finished_at",
            },
        )
        encoded = json.dumps(output)
        for forbidden in (
            self.key,
            str(self.path),
            "Demo market",
            "secret-looking-request",
            "demo-bank",
            "250.00",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_lock_contention_completes_safely(self) -> None:
        with sync_lock(self.path.with_suffix(".worker.lock")):
            self.assertEqual(
                execute_task(self.service, SyncTask("locked"))["status"],
                "already_running",
            )
        with sync_lock(self.path.with_suffix(".sync.lock")):
            self.assertEqual(
                execute_task(self.service, SyncTask("cli-locked"))["status"],
                "already_running",
            )

    def test_retry_policy_and_exception_redaction(self) -> None:
        for code, retryable in (
            ("transient", True),
            ("authentication", False),
            ("validation", False),
        ):
            with patch.object(
                self.service,
                "sync",
                return_value=SyncResult(status="failed", errors=[code]),
            ):
                output = execute_task(self.service, SyncTask(code))
            self.assertEqual(output["retryable"], retryable)
        with patch.object(
            self.service, "sync", side_effect=RuntimeError("private response body")
        ):
            output = execute_task(self.service, SyncTask("failure"))
        self.assertEqual(output["error"], "local_failure")
        self.assertNotIn("private", json.dumps(output))
        self.assertEqual(
            [WorkerSettings().retry_delay(n) for n in (1, 2, 3)], [60, 120, 240]
        )
        self.assertEqual(WorkerSettings().retry_delay(100), 3600)

    def test_invalid_request_is_rejected_before_sync(self) -> None:
        with patch.object(self.service, "sync") as sync:
            output = execute_task(self.service, SyncTask("bad\nrequest"))
            self.assertEqual(output["error"], "validation")
            sync.assert_not_called()

    def test_single_worker_run_writes_only_sanitized_logs_and_heartbeat(self) -> None:
        result = run_demo_worker(self.service, once=True)
        self.assertEqual(result["status"], "completed")
        logs = (self.directory / "logs" / "worker.log").read_text()
        self.assertNotIn(self.key, logs)
        self.assertNotIn("Demo market", logs)
        self.assertNotIn("419.90", logs)
        self.assertEqual(
            json.loads((self.directory / "worker-state.json").read_text())["adapter"],
            "fake",
        )

    def test_logger_discards_extra_sensitive_fields(self) -> None:
        stream = io.StringIO()
        logger = logging.getLogger("finance.test-output")
        handler = logging.StreamHandler(stream)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            log_result(
                logger,
                "private-request",
                {
                    "status": "completed",
                    "merchant": "private-merchant",
                    "token": "private-token",
                },
                1.0,
            )
        finally:
            logger.removeHandler(handler)
        self.assertNotIn("private", stream.getvalue())

    def test_worker_retries_only_transient_failures(self) -> None:
        for retryable, calls in ((True, 2), (False, 1)):
            ticks = count(0, 100)
            with (
                patch(
                    "finance.worker.run_once",
                    return_value={
                        "status": "failed",
                        "error": "transient" if retryable else "authentication",
                        "retryable": retryable,
                    },
                ) as run,
                patch("finance.worker.time.monotonic", side_effect=ticks),
                patch(
                    "finance.worker.time.sleep", side_effect=[None, KeyboardInterrupt]
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                run_demo_worker(
                    self.service, settings=WorkerSettings(retry_base_seconds=1)
                )
            self.assertEqual(run.call_count, calls)

    def test_monthly_demo_refreshes_for_long_lived_worker(self) -> None:
        provider = MonthlyDemoProvider()
        first = provider.fetch_transactions(
            "demo-bank", NOW.replace(day=1, hour=0), NOW
        )
        later = NOW.replace(month=10)
        second = provider.fetch_transactions(
            "demo-bank", later.replace(day=1, hour=0), later
        )
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertTrue(all(record.transaction_date.month == 10 for record in second))


class InstallerTests(unittest.TestCase):
    def test_guided_demo_end_to_end_without_real_keychain_or_service(self) -> None:
        answers = iter(["y", "n"])
        store = FakeSecretStore()
        with tempfile.TemporaryDirectory() as directory:
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                self.assertEqual(
                    setup_demo(Path(directory), store, ask=lambda _: next(answers)), 0
                )
            self.assertIn("419.90", stream.getvalue())
            self.assertTrue((Path(directory) / "demo" / "finance.db").exists())

    def test_declining_initial_import_does_not_start_service_or_import(self) -> None:
        answers = iter(["n"])
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("finance.install.start_launch_agent") as start,
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    setup_demo(
                        Path(directory), FakeSecretStore(), ask=lambda _: next(answers)
                    ),
                    1,
                )
            start.assert_not_called()
            self.assertFalse((Path(directory) / "demo" / "finance.db").exists())

    def test_background_configuration_is_reviewable_and_secret_free(self) -> None:
        answers = iter(["y", "y"])
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("finance.install.start_launch_agent") as start,
        ):
            path = Path(directory)
            with contextlib.redirect_stdout(io.StringIO()):
                result = setup_demo(
                    path,
                    FakeSecretStore(),
                    ask=lambda _: next(answers),
                    launch_agent=path / "worker.plist",
                )
            self.assertEqual(result, 0)
            start.assert_called_once()
            definition = plistlib.loads((path / "worker.plist").read_bytes())
            self.assertTrue(definition["RunAtLoad"])
            self.assertTrue(definition["KeepAlive"])
            self.assertNotIn("key", json.dumps(definition).casefold())
            self.assertIn("--demo", definition["ProgramArguments"])

    def test_plist_safely_encodes_spaces_and_special_characters(self) -> None:
        definition = launchd_definition(
            Path("/test space/a&b/python"), Path("/private app")
        )
        encoded = plistlib.dumps(definition)
        self.assertEqual(plistlib.loads(encoded), definition)

    def test_live_setup_goes_directly_from_mode_to_credentials(self) -> None:
        stream = io.StringIO()
        with (
            contextlib.redirect_stdout(stream),
            patch("finance.install.sys.platform", "darwin"),
            patch("builtins.input", side_effect=["2"]),
            patch("finance.install.MacOSKeychain", return_value=FakeSecretStore()),
            patch(
                "finance.install.connect_interactively",
                return_value={
                    "account_count": 2,
                    "connections_requiring_attention": 1,
                },
            ) as connect,
        ):
            self.assertEqual(install_main([]), 0)
        connect.assert_called_once()
        self.assertIn("Live transaction import is not enabled", stream.getvalue())
        self.assertIn("2 accounts", stream.getvalue())
        self.assertIn("renew bank permissions", stream.getvalue())

    def test_rerunning_setup_preserves_existing_key_and_data(self) -> None:
        store = FakeSecretStore()
        with tempfile.TemporaryDirectory() as directory:
            for _ in range(2):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = setup_demo(
                        Path(directory),
                        store,
                        ask=Mock(side_effect=["y", "n"]),
                    )
                self.assertEqual(result, 0)
