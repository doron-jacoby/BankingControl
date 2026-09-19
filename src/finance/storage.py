"""SQLCipher-only storage; opening never creates or replaces a database."""

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from importlib.resources import files
from pathlib import Path

from sqlcipher3 import dbapi2 as sqlcipher

from finance.security import validate_key

DEFAULT_DATABASE_PATH = (
    Path.home() / "Library" / "Application Support" / "PersonalFinance" / "finance.db"
)


class StorageError(RuntimeError):
    """Sanitized storage failure without paths, queries, records, or keys."""


def _decimal_text(amount: Decimal) -> str:
    if not amount.is_finite():
        raise ValueError("Amount must be a finite Decimal")
    return str(amount)


def _reject_float(value: float) -> str:
    raise ValueError("Binary floats are not supported in financial storage")


sqlcipher.register_adapter(Decimal, _decimal_text)
sqlcipher.register_adapter(float, _reject_float)
sqlcipher.register_converter("DECIMAL_TEXT", lambda value: Decimal(value.decode()))


def _prepare_path(path: Path, *, create: bool) -> None:
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
    ):
        raise StorageError("Database directory must be private and owned by this user")
    if create:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise StorageError("Database must be a private regular file owned by this user")


@contextmanager
def open_database(
    key: str,
    path: Path = DEFAULT_DATABASE_PATH,
    *,
    create: bool = False,
) -> Iterator[sqlcipher.Connection]:
    """Create explicitly, or open schema v1. Commit on success, rollback on error.

    The caller obtains a key from Keychain. No missing-key recovery or plaintext
    fallback occurs here. Keep the database in its dedicated private directory.
    """
    validate_key(key)
    connection = None
    try:
        _prepare_path(path, create=create)
        connection = sqlcipher.connect(
            path, detect_types=sqlcipher.PARSE_DECLTYPES, timeout=30
        )
        # PRAGMA cannot bind parameters; validate_key permits hex digits only.
        connection.execute(f"PRAGMA key = \"x'{key}'\"")
        if not connection.execute("PRAGMA cipher_version").fetchone():
            raise StorageError("SQLCipher encryption is unavailable")
        connection.execute("PRAGMA cipher_memory_security = ON")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        if create:
            connection.executescript(
                files("finance").joinpath("schema.sql").read_text(encoding="utf-8")
            )
        if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise StorageError("Unsupported database schema version")
        # DELETE mode permits a closed-file backup without separate WAL files.
        connection.execute("PRAGMA journal_mode = DELETE")
        with connection:
            yield connection
    except (sqlcipher.Error, OSError):
        raise StorageError("Encrypted database operation failed") from None
    finally:
        if connection is not None:
            connection.close()
