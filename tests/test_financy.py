import contextlib
import io
import json
import unittest
from collections import deque
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
)
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


CREDS = Credentials("synthetic-client", "synthetic-secret", "synthetic-user")


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
                read_secret=Mock(side_effect=CREDS.payload().values()),
                transport=transport,
            )
        self.assertEqual(summary["account_count"], 1)
        self.assertEqual(summary["connections_requiring_attention"], 1)
        self.assertEqual(Credentials.load(store), CREDS)
        self.assertNotIn(CREDS.clientSecret, stream.getvalue())
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(FinancyError):
            connect_interactively(
                store,
                read_secret=Mock(side_effect=["new-client", "new-secret", "new-user"]),
                transport=FakeTransport([(401, {})]),
            )
        self.assertEqual(Credentials.load(store), CREDS)

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
