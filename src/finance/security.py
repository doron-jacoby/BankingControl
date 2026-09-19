"""Explicit Keychain access: no automatic plaintext backend or key replacement."""

import re
import secrets
import sys
from typing import Protocol

SERVICE = "PersonalFinance"
DATABASE_KEY = "database-key-v1"


class SecretStore(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...


class SecretError(RuntimeError):
    """Sanitized failure safe to present without the backend exception."""


class MacOSKeychain:
    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise SecretError("macOS Keychain is required")
        from keyring.backends.macOS import Keyring

        self._backend: SecretStore = Keyring()  # type: ignore[no-untyped-call]

    def get_password(self, service: str, username: str) -> str | None:
        try:
            return self._backend.get_password(service, username)
        except Exception:
            raise SecretError("Keychain read failed") from None

    def set_password(self, service: str, username: str, password: str) -> None:
        try:
            self._backend.set_password(service, username, password)
        except Exception:
            raise SecretError("Keychain write failed") from None


class FakeSecretStore:
    """In-memory test double; never a production fallback."""

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._values[service, username] = password


def validate_key(key: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{64}", key):
        raise SecretError("Database key must contain exactly 32 bytes of hex data")
    return key


def load_database_key(store: SecretStore) -> str:
    key = store.get_password(SERVICE, DATABASE_KEY)
    if key is None:
        raise SecretError("Database key missing; restore Keychain before opening")
    return validate_key(key)


def create_database_key(store: SecretStore) -> str:
    """Installation only, before DB creation. Never overwrites an existing key."""
    if store.get_password(SERVICE, DATABASE_KEY) is not None:
        raise SecretError("Database key already exists")
    key = secrets.token_hex(32)
    store.set_password(SERVICE, DATABASE_KEY, key)
    if load_database_key(store) != key:
        raise SecretError("Database key could not be verified")
    return key
