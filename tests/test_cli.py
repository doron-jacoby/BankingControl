import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from finance.cli import main
from finance.security import FakeSecretStore, create_database_key
from finance.storage import open_database


class CLITests(unittest.TestCase):
    def test_demo_cli_sync_status_and_local_records(self) -> None:
        store = FakeSecretStore()
        key = create_database_key(store, "database-key-demo-v1")
        with tempfile.TemporaryDirectory() as directory:
            with open_database(
                key, Path(directory) / "demo" / "finance.db", create=True
            ):
                pass
            prefix = ["--demo", "--data-dir", directory]
            with patch("finance.cli.MacOSKeychain", return_value=store):
                for command in ("sync", "status", "accounts", "transactions", "worker"):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        suffix = ["--once"] if command == "worker" else []
                        self.assertEqual(main(prefix + [command] + suffix), 0)
                    data = json.loads(output.getvalue())
                    if command == "sync":
                        self.assertEqual(data["inserted"], 13)
                    elif command == "status":
                        self.assertEqual(data["transaction_count"], 13)
                        self.assertTrue(data["encrypted_db_available"])
                    else:
                        self.assertTrue(data)
                    self.assertNotIn(key, output.getvalue())

    def test_live_mode_requires_contract_and_missing_demo_is_clear(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["sync"]), 2)
            with tempfile.TemporaryDirectory() as directory:
                self.assertEqual(main(["--demo", "--data-dir", directory, "status"]), 1)
