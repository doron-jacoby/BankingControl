"""Verified Financy API 1.0.0 authentication and read-only account discovery.

Contract: docs-financy.open-finance.ai/reference/{createtoken,getaccounts,
getconnections}. Transaction status semantics remain unverified; this client
does not guess a FinanceProvider transaction mapping.
"""

import getpass
import http.client
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from finance.models import Account
from finance.security import SERVICE, SecretStore

CREDENTIAL_NAME = "financy-credentials-v1"
API_HOST = "api.open-finance.ai"
READ_PATHS = {"/v2/connections", "/v2/data/accounts"}
READABLE_STATUSES = {"ACTIVE", "CONNECTED", "COMPLETED"}
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class FinancyError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = (
            code
            if code
            in {
                "authentication",
                "plan",
                "forbidden",
                "transient",
                "validation",
                "not_configured",
            }
            else "validation"
        )
        super().__init__(self.code)


@dataclass(frozen=True, repr=False)
class Credentials:
    clientId: str
    clientSecret: str
    userId: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip() or len(value) > 8192
            for value in (self.clientId, self.clientSecret, self.userId)
        ):
            raise FinancyError("validation")

    def payload(self) -> dict[str, str]:
        return {
            "clientId": self.clientId,
            "clientSecret": self.clientSecret,
            "userId": self.userId,
        }

    @classmethod
    def load(cls, store: SecretStore) -> "Credentials":
        raw = store.get_password(SERVICE, CREDENTIAL_NAME)
        if raw is None:
            raise FinancyError("not_configured")
        try:
            return cls(**json.loads(raw))
        except (TypeError, ValueError):
            raise FinancyError("validation") from None


type Transport = Callable[
    [str, str, dict[str, str] | None, str | None], tuple[int, dict[str, Any]]
]


def https_request(
    method: str, path: str, payload: dict[str, str] | None, token: str | None
) -> tuple[int, dict[str, Any]]:
    """Fixed TLS host, explicit routes, no redirects, raw logs or proxy variables."""
    route = path.split("?", 1)[0]
    if not (
        method == "POST"
        and route == "/oauth/token"
        or method == "GET"
        and route in READ_PATHS
    ):
        raise FinancyError("validation")
    connection = http.client.HTTPSConnection(API_HOST, timeout=30)
    try:
        headers = {
            "Accept": "application/json",
            "User-Agent": "PersonalFinanceMonitor/0.1",
        }
        if token:
            headers["Authorization"] = "Bearer " + token
        body = json.dumps(payload).encode() if payload is not None else None
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise FinancyError("validation")
        try:
            parsed = json.loads(data, parse_float=Decimal)
        except (ValueError, UnicodeError):
            if response.status != 200:
                return response.status, {}
            raise FinancyError("validation") from None
        if not isinstance(parsed, dict):
            raise FinancyError("validation")
        return response.status, parsed
    except (OSError, http.client.HTTPException):
        raise FinancyError("transient") from None
    finally:
        connection.close()


def check_response(status: int, body: dict[str, Any]) -> None:
    if status == 200:
        return
    if status == 401:
        raise FinancyError("authentication")
    if status == 403:
        raise FinancyError(
            "plan" if body.get("type") == "NOT_AVAILABLE_ON_PLAN" else "forbidden"
        )
    if status == 429 or status >= 500 or body.get("type") == "PROVIDER_UNAVAILABLE":
        raise FinancyError("transient")
    raise FinancyError("validation")


def required_text(row: dict[str, Any], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise FinancyError("validation")
    return value


class FinancyClient:
    def __init__(
        self, credentials: Credentials, transport: Transport = https_request
    ) -> None:
        self._credentials = credentials
        self._transport = transport
        self._token: str | None = None
        self._expires_at = 0.0

    def _authenticate(self) -> None:
        status, body = self._transport(
            "POST", "/oauth/token", self._credentials.payload(), None
        )
        check_response(status, body)
        token = required_text(body, "accessToken")
        expiry = body.get("expiresIn")
        if (
            body.get("tokenType") != "Bearer"
            or "\n" in token
            or "\r" in token
            or isinstance(expiry, bool)
            or not isinstance(expiry, int | Decimal)
            or not 0 < expiry <= 31_536_000
        ):
            raise FinancyError("validation")
        self._token = token
        self._expires_at = time.monotonic() + float(expiry) * 0.9

    def _pages(self, path: str) -> list[dict[str, Any]]:
        from urllib.parse import urlencode

        if path not in READ_PATHS:
            raise FinancyError("validation")
        result = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            query = {"limit": "100"}
            if path == "/v2/data/accounts":
                query["includeDuplicates"] = "0"
            if cursor is not None:
                query["nextPage"] = cursor
            if self._token is None or time.monotonic() >= self._expires_at:
                self._authenticate()
            route = path + "?" + urlencode(query)
            status, body = self._transport("GET", route, None, self._token)
            if status == 401:
                self._authenticate()
                status, body = self._transport("GET", route, None, self._token)
            check_response(status, body)
            items = body.get("items")
            if not isinstance(items, list) or any(
                not isinstance(item, dict) for item in items
            ):
                raise FinancyError("validation")
            result.extend(items)
            cursor = body.get("nextPage")
            if cursor is None or cursor == "":
                return result
            if not isinstance(cursor, str) or cursor in seen or len(seen) >= 10_000:
                raise FinancyError("validation")
            seen.add(cursor)

    def list_accounts(self) -> list[Account]:
        types = {
            "CHECKING": "checking",
            "CARD": "credit_card",
            "LOAN": "loan",
            "SAVINGS": "savings",
            "SECURITY": "investment",
        }
        result = []
        for row in self._pages("/v2/data/accounts"):
            kind = required_text(row, "accountType")
            if kind not in types:
                raise FinancyError("validation")
            institution = required_text(row, "providerId")
            result.append(
                Account(
                    provider="financy",
                    provider_account_id=required_text(row, "id"),
                    connection_id=required_text(row, "connectionId"),
                    institution=institution,
                    account_type=types[kind],
                    display_name=f"{institution} {types[kind]}",
                    currency=required_text(row, "currency"),
                )
            )
        # accountName/accountNumber, ownerInfo, card details and balances are
        # deliberately not retained; labels must not accidentally contain a PAN.
        return result

    def connection_summary(self) -> dict[str, int]:
        rows = self._pages("/v2/connections")
        readable = sum(
            required_text(row, "status") in READABLE_STATUSES for row in rows
        )
        return {
            "connections": len(rows),
            "readable_connections": readable,
            "connections_requiring_attention": len(rows) - readable,
        }


def connect_interactively(
    store: SecretStore,
    *,
    read_secret: Callable[[str], str] | None = None,
    transport: Transport = https_request,
) -> dict[str, str | int]:
    if read_secret is None:
        if not sys.stdin.isatty():
            raise FinancyError("validation")
        read_secret = getpass.getpass
    print("Financy → Settings → API. Enter the three values locally; input is hidden.")
    credentials = Credentials(
        clientId=read_secret("clientId: ").strip(),
        clientSecret=read_secret("clientSecret: ").strip(),
        userId=read_secret("userId: ").strip(),
    )
    client = FinancyClient(credentials, transport)
    summary = client.connection_summary()
    discovered = client.list_accounts()
    # One Keychain item avoids partially updating three separate credentials.
    store.set_password(SERVICE, CREDENTIAL_NAME, json.dumps(credentials.payload()))
    if Credentials.load(store) != credentials:
        raise FinancyError("validation")
    return {
        "provider": "financy",
        "status": "connected" if discovered else "no_linked_accounts",
        "account_count": len(discovered),
        **summary,
    }
