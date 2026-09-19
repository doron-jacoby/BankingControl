# Personal Finance Monitor

Local, read-only Python application based on
[the implementation specification](PERSONAL_FINANCE_MONITOR_SPEC.md).

## Development

Python 3.12+ is required. On this Mac Python 3.14 is available at
`/opt/homebrew/bin/python3.14`; the system Python 3.9 is too old.

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/python -m unittest discover -s tests -v
```

Tests use synthetic records, temporary encrypted databases, and an in-memory
Keychain fake. They require no bank, Financy, Keychain, or Conductor credentials.
Runtime dependencies are [sqlcipher3](https://github.com/coleifer/sqlcipher3)
and [keyring](https://keyring.readthedocs.io/en/stable/). The Keychain adapter
selects the macOS backend explicitly, with no plaintext fallback.

## Storage and recovery

The default database is
`~/Library/Application Support/PersonalFinance/finance.db`.
SQLCipher uses a random 256-bit key, stored under service `PersonalFinance`,
account `database-key-v1` in the user's macOS Keychain. Opening a database never
generates a replacement key. Missing keys, unsupported schema versions, wrong
keys, and unsafe file permissions fail closed.

Amounts use `Decimal`, stored with TEXT affinity; never use SQLite `SUM(amount)`
or floating-point conversions for financial totals. Debits are negative and
credits positive. Provider adapters must normalize this convention explicitly.
Metadata is for necessary sanitized attributes, not entire response bodies.

To back up: stop all workers and CLI operations, close all database connections,
and copy the encrypted database file to a protected backup location. Journal
mode is DELETE; do not copy a live database. Keep an encrypted macOS backup that
includes your login Keychain and its recovery credentials. Do not assume this
application's generic Keychain item syncs through iCloud. Test restoration on a
separate local copy before relying on a backup.

To restore: stop the app, restore the matching Keychain item through your macOS
Keychain recovery process, restore the closed encrypted DB with directory mode
0700 and file mode 0600, and reopen with the original key. Without that key the
database cannot be recovered. Never export an unencrypted database or place the
key beside the backup, in `.env`, or in this repository.

## Implementation progress

| Phase | State | Verification |
| --- | --- | --- |
| 1 — foundation | Complete: package, models, SQLCipher schema, Keychain adapter, fake provider | 15 tests; Ruff formatting/lint and strict mypy passed |
| 2 — provider | Real integration awaits official Financy contract/version | Fake adapter available |
| 3–5 | In progress | Results recorded as implemented |
| 6 — local AI | Deferred until the real core is stable | Outside v1 |

Financy endpoints, consent flows, banking permission screens, Conductor SDK
methods, and deployment APIs will not be guessed. Netflix Conductor (the
orchestrator) is separate from the Conductor desktop app used for development.
