import contextlib
import io
import json
import os
import pty
import select
import signal
import sys
import termios
import unittest
from collections import deque
from decimal import Decimal
from typing import Any
from unittest.mock import Mock, patch

from finance.cli import main
from finance.financy import (
    CREDENTIAL_NAME,
    Credentials,
    FinancyClient,
    FinancyError,
    check_response,
    connect_interactively,
    https_request,
    masked_input,
)
from finance.install import main as install_main
from finance.security import SERVICE, FakeSecretStore


class FakeTransport:
    def __init__(self, responses: list[tuple[int, dict[str, Any]]]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, str, dict[str, str] | None, str | None]] = []

    def __call__(
        self, method: str, path: str, payload: dict[str, str] | None, token: str | None
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append((method, path, payload, token))
        return self.responses.popleft()


def token() -> tuple[int, dict[str, Any]]:
    return 200, {
        "accessToken": "synthetic-token",
        "tokenType": "Bearer",
        "expiresIn": 86400,
    }


def account_page(cursor: str | None = None) -> tuple[int, dict[str, Any]]:
    return 200, {
        "nextPage": cursor,
        "items": [
            {
                "id": "synthetic-account",
                "providerId": "leumi",
                "connectionId": "synthetic-connection",
                "accountType": "CHECKING",
                "currency": "ILS",
                "accountName": "private-card-number",
                "accountNumber": "private-number",
                "ownerInfo": {"nationalId": "private-national-id"},
                "balances": [{"amount": 12345}],
            }
        ],
    }


CREDS = Credentials("c" * 32, "s" * 64, "synthetic-user")


class FinancyTests(unittest.TestCase):
    def test_authentication_pagination_and_account_minimization(self) -> None:
        transport = FakeTransport([token(), account_page("next+/"), account_page()])
        accounts = FinancyClient(CREDS, transport).list_accounts()
        self.assertEqual(len(accounts), 2)
        self.assertEqual(
            transport.calls[0], ("POST", "/oauth/token", CREDS.payload(), None)
        )
        self.assertIn("nextPage=next%2B%2F", transport.calls[2][1])
        self.assertEqual(accounts[0].account_type, "checking")
        self.assertEqual(accounts[0].metadata, {})
        self.assertNotIn("private", accounts[0].display_name)
        self.assertNotIn("synthetic-secret", repr(CREDS))

    def test_expired_token_reauthenticates_once(self) -> None:
        transport = FakeTransport([token(), (401, {}), token(), account_page()])
        self.assertEqual(len(FinancyClient(CREDS, transport).list_accounts()), 1)
        self.assertEqual(
            [call[0] for call in transport.calls], ["POST", "GET", "POST", "GET"]
        )
        refused = FakeTransport(
            [token(), (401, {}), token(), (401, {"message": "private"})]
        )
        with self.assertRaises(FinancyError) as error:
            FinancyClient(CREDS, refused).list_accounts()
        self.assertEqual(str(error.exception), "authentication")

    def test_documented_securities_account_type_is_supported(self) -> None:
        status, page = account_page()
        page["items"][0]["accountType"] = "SECURITIES"
        accounts = FinancyClient(
            CREDS, FakeTransport([token(), (status, page)])
        ).list_accounts()
        self.assertEqual(accounts[0].account_type, "investment")

    def test_error_codes_never_expose_server_messages(self) -> None:
        for status, body, expected in [
            (403, {"type": "NOT_AVAILABLE_ON_PLAN"}, "plan"),
            (403, {}, "forbidden"),
            (429, {}, "transient"),
            (503, {}, "transient"),
            (400, {"type": "PROVIDER_UNAVAILABLE"}, "transient"),
            (302, {}, "validation"),
            (400, {}, "validation"),
        ]:
            with self.subTest(status=status), self.assertRaises(FinancyError) as error:
                check_response(status, body | {"message": "private-response"})
            self.assertEqual(str(error.exception), expected)

    def test_repeated_cursor_and_malformed_items_fail_closed(self) -> None:
        for pages in (
            [account_page("same"), account_page("same")],
            [(200, {"items": "invalid"})],
        ):
            with self.assertRaises(FinancyError):
                FinancyClient(CREDS, FakeTransport([token(), *pages])).list_accounts()

    def test_read_credentials_from_keychain_only(self) -> None:
        store = FakeSecretStore()
        with self.assertRaises(FinancyError):
            Credentials.load(store)
        store.set_password(SERVICE, CREDENTIAL_NAME, json.dumps(CREDS.payload()))
        self.assertEqual(Credentials.load(store), CREDS)

    def test_interactive_setup_verifies_before_storing_credentials(self) -> None:
        store = FakeSecretStore()
        connections = (
            200,
            {"items": [{"status": "ACTIVE"}, {"status": "EXPIRED"}], "nextPage": None},
        )
        transport = FakeTransport([token(), connections, account_page()])
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            summary = connect_interactively(
                store,
                read_secret=Mock(
                    side_effect=[CREDS.userId, CREDS.clientId, CREDS.clientSecret]
                ),
                transport=transport,
            )
        self.assertEqual(summary["account_count"], 1)
        self.assertEqual(summary["connections_requiring_attention"], 1)
        self.assertEqual(Credentials.load(store), CREDS)
        self.assertNotIn(CREDS.clientSecret, stream.getvalue())
        self.assertNotIn("characters)", stream.getvalue())
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(FinancyError):
            connect_interactively(
                store,
                read_secret=Mock(side_effect=["new-user", "n" * 32, "t" * 64]),
                transport=FakeTransport([(401, {})]),
            )
        self.assertEqual(Credentials.load(store), CREDS)

    def test_credentials_follow_site_order_and_wrong_lengths_are_retried(self) -> None:
        store = FakeSecretStore()
        stream = io.StringIO()
        transport = FakeTransport([token(), (200, {"items": []}), account_page()])
        reader = Mock(
            side_effect=[
                "",
                "\x1b[31m",
                " u ",
                "c" * 31,
                "c" * 33,
                CREDS.clientId,
                "s" * 63,
                "s" * 65,
                CREDS.clientSecret,
            ]
        )
        with contextlib.redirect_stdout(stream):
            connect_interactively(
                store,
                read_secret=reader,
                transport=transport,
            )
        self.assertEqual(
            Credentials.load(store),
            Credentials(CREDS.clientId, CREDS.clientSecret, "u"),
        )
        output = stream.getvalue()
        self.assertIn("Expected 32 characters", output)
        self.assertIn("Expected 64 characters", output)
        self.assertNotIn(CREDS.clientId[:8], output)
        self.assertNotIn(CREDS.clientSecret[:8], output)
        self.assertNotIn("\x1b", output)
        self.assertEqual(
            [call.args[0] for call in reader.call_args_list],
            ["[1/3] User ID: "] * 3
            + ["[2/3] Client ID: "] * 3
            + ["[3/3] Client secret: "] * 3,
        )

    def test_millisecond_token_lifetime_and_early_renewal(self) -> None:
        for expiry, lifetime in [(86_400_000, 77_760), (86400, 77.76)]:
            transport = FakeTransport(
                [
                    (200, token()[1] | {"expiresIn": expiry}),
                    account_page(),
                    token(),
                    account_page(),
                ]
            )
            client = FinancyClient(CREDS, transport)
            with patch("finance.financy.time.monotonic", return_value=100):
                self.assertEqual(len(client.list_accounts()), 1)
            self.assertAlmostEqual(client._expires_at, 100 + lifetime)
            with patch("finance.financy.time.monotonic", return_value=101 + lifetime):
                client.list_accounts()
            self.assertEqual(
                [call[0] for call in transport.calls], ["POST", "GET", "POST", "GET"]
            )
        for invalid_expiry in [
            0,
            -1,
            True,
            "86400",
            Decimal("NaN"),
            Decimal("Infinity"),
        ]:
            with (
                self.subTest(expiry=invalid_expiry),
                self.assertRaises(FinancyError) as error,
            ):
                FinancyClient(
                    CREDS,
                    FakeTransport([(200, token()[1] | {"expiresIn": invalid_expiry})]),
                ).list_accounts()
            self.assertIn("/oauth/token", error.exception.detail)

    def test_diagnostics_identify_endpoint_without_response_values(self) -> None:
        for responses, expected in [
            ([(400, {"message": "private-secret"})], "/oauth/token: HTTP 400"),
            (
                [token(), (403, {"message": "private-secret"})],
                "/v2/data/accounts: HTTP 403",
            ),
            (
                [token(), (200, {"items": "private-secret"})],
                "/v2/data/accounts: missing or invalid items list",
            ),
        ]:
            with self.assertRaises(FinancyError) as error:
                FinancyClient(CREDS, FakeTransport(responses)).list_accounts()
            self.assertEqual(error.exception.detail, expected)
            self.assertNotIn("private", error.exception.detail)

    def test_installer_displays_safe_failure_detail(self) -> None:
        stream = io.StringIO()
        with (
            contextlib.redirect_stdout(stream),
            patch("finance.install.sys.platform", "darwin"),
            patch("builtins.input", return_value="2"),
            patch("finance.install.MacOSKeychain", return_value=FakeSecretStore()),
            patch(
                "finance.install.connect_interactively",
                side_effect=FinancyError("validation", detail="/oauth/token: HTTP 400"),
            ),
        ):
            self.assertEqual(install_main([]), 1)
        self.assertIn("/oauth/token: HTTP 400", stream.getvalue())

    def test_masked_paste_and_terminal_restoration(self) -> None:
        # Real pseudo-terminal: stars must appear BEFORE Enter, including on
        # Python 3.12/3.13's compatibility path. Never use real credentials here.
        for version in [(3, 13), sys.version_info]:
            for cancel in [False, True]:
                with self.subTest(version=version, cancel=cancel):
                    child, descriptor = pty.fork()
                    if child == 0:
                        try:
                            original = termios.tcgetattr(0)
                            with patch("finance.financy.sys.version_info", version):
                                try:
                                    value = masked_input("Credential: ")
                                    valid = not cancel and value == "fake-valuX"
                                except KeyboardInterrupt:
                                    valid = cancel
                            valid = valid and termios.tcgetattr(0) == original
                            print("RESTORED" if valid else "FAILED", flush=True)
                            os._exit(0)
                        except BaseException:
                            os._exit(1)
                    captured = bytearray()

                    def wait_for(
                        marker: bytes,
                        captured: bytearray = captured,
                        descriptor: int = descriptor,
                    ) -> None:
                        while marker not in captured:
                            self.assertTrue(
                                select.select([descriptor], [], [], 5)[0],
                                "terminal timed out",
                            )
                            captured.extend(os.read(descriptor, 4096))

                    try:
                        wait_for(b"Credential: ")
                        os.write(descriptor, b"fake-value")
                        wait_for(b"**********")
                        self.assertNotIn(b"fake-value", captured)
                        os.write(descriptor, b"\x03" if cancel else b"\x7fX\n")
                        wait_for(b"RESTORED")
                    finally:
                        # Ensure a failed assertion never leaves a child waiting.
                        try:
                            os.kill(child, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        os.waitpid(child, 0)
                        os.close(descriptor)

    def test_noninteractive_setup_never_falls_back_to_echoed_secret_input(self) -> None:
        with (
            patch("finance.financy.sys.stdin.isatty", return_value=False),
            self.assertRaises(FinancyError),
        ):
            connect_interactively(FakeSecretStore())

    def test_transport_allows_only_authentication_and_approved_reads(self) -> None:
        for method, path in [
            ("POST", "/v2/payments"),
            ("POST", "/chat/chat/connections/refresh"),
            ("DELETE", "/v2/connections"),
            ("GET", "https://other.example/"),
        ]:
            with self.assertRaises(FinancyError):
                https_request(method, path, None, "synthetic-token")

    def test_redirect_is_not_followed_and_network_errors_are_sanitized(self) -> None:
        with patch("finance.financy.http.client.HTTPSConnection") as factory:
            response = factory.return_value.getresponse.return_value
            response.status = 302
            response.read.return_value = b""
            self.assertEqual(
                https_request("GET", "/v2/data/accounts", None, "synthetic-token"),
                (302, {}),
            )
            factory.assert_called_once_with("api.open-finance.ai", timeout=30)
            factory.return_value.request.side_effect = OSError("private-token-in-error")
            with self.assertRaises(FinancyError) as error:
                https_request("GET", "/v2/data/accounts", None, "synthetic-token")
            self.assertEqual(str(error.exception), "transient")

    def test_live_cli_discovery_without_real_keychain(self) -> None:
        store = FakeSecretStore()
        store.set_password(SERVICE, CREDENTIAL_NAME, json.dumps(CREDS.payload()))
        transport = FakeTransport([token(), account_page()])
        with (
            patch("finance.cli.MacOSKeychain", return_value=store),
            patch(
                "finance.cli.FinancyClient",
                return_value=FinancyClient(CREDS, transport),
            ),
        ):
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                self.assertEqual(main(["accounts"]), 0)
            self.assertIn("financy", stream.getvalue())
            self.assertNotIn("private", stream.getvalue())
