# Personal Finance Monitor

Local, read-only Python application based on
[the implementation specification](PERSONAL_FINANCE_MONITOR_SPEC.md).

## Start here

```sh
cd /Users/doron/source/BankingControl
./install.sh
```

The English wizard checks macOS/Python and installs an isolated runtime.
Demo setup asks once before creating encrypted storage and importing data,
then runs **the same worker once in the foreground** to check
permissions and show a short monthly summary. It can then install and verify a
persistent demo LaunchAgent. The installed runtime is independent of this
checkout, under `~/Library/Application Support/PersonalFinance/runtime`.

The full import/report/worker flow uses synthetic data. The second setup option
now supports **live Financy authentication and account discovery** against the
published API 1.0.0 contract. It guides you through signing in, linking your bank
in Financy and finding API credentials at the bottom of Settings.
Paste each value and press Enter: a masked prefix (up to eight characters) and
character count confirm receipt. Full values remain hidden. Credentials are
verified before being stored as one macOS Keychain item. There are no extra
Enter confirmations between instructions.

Live transaction import remains disabled: the published transaction schema does
not enumerate status values or define pending-to-final links. Those semantics
must be confirmed before real transactions can safely enter expense totals.
Production Netflix Conductor integration also still needs its version/SDK.
See [the verified Financy contract and remaining gaps](docs/FINANCY_CONTRACT.md).

`./install.sh --check` checks prerequisites without installing anything.
`./install.sh --demo` skips the mode choice but still asks before the first import.
An explicit `FINANCE_PYTHON=/path/to/python3.12` can select your Python.
Installer reruns reuse the existing key and database. Keychain permission
prompts must be completed interactively; the automated tests use fakes.

After installation:

```sh
finance="$HOME/Library/Application Support/PersonalFinance/runtime/bin/finance"
"$finance" connect             # Live Financy credential setup, local hidden input
"$finance" accounts            # Read actual Financy accounts, local output only
"$finance" status              # Verify API access and connection readiness
"$finance" --demo status
"$finance" --demo sync
"$finance" --demo accounts
"$finance" --demo transactions --days 30
"$finance" --demo monthly 2026-09
"$finance" --demo classify --all
"$finance" --demo classify --from 2026-01-01
"$finance" --demo rule merchant_exact 'Demo market' HEALTH --priority 10
"$finance" --demo worker --once
```

Use the current month for the bundled demo. Monthly totals are separated by
currency and use Asia/Jerusalem by default (`monthly --timezone UTC` is also
supported). See the [synthetic audit report](docs/AUDIT_EXAMPLE.md) for exact
inclusions, exclusions and reconciliation limits. Manual rules override
automatic categories, and any explicitly supplied flags override derived flags.

The demo worker polls locally and generates one request per ISO week; it does
not talk to a server. It catches up on the first poll after waking. Logs rotate
at 1 MB with three backups and contain only operational metadata. A recent
heartbeat in `status` indicates recent polling, not proof of server connectivity.
The LaunchAgent requires the user to be logged in and the Mac awake. Production
Conductor scheduling remains blocked on the external contract; see
[the integration plans](conductor/README.md).

To stop the demo background worker:

```sh
launchctl bootout "gui/$(id -u)/com.personalfinance.demo-worker"
```

For permanent removal, also remove
`~/Library/LaunchAgents/com.personalfinance.demo-worker.plist`. Keep the encrypted
database and its Keychain item unless you deliberately intend to delete data.

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
The input boundary limits amounts to 100 digits and exponents within ±100 to
reject pathological payloads without rounding normal financial values.
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

Demo data uses a separate `demo/finance.db` and Keychain account
`database-key-demo-v1`. No real data is mixed into it. Database creation/opening
uses the [documented SQLCipher keying and validation sequence](https://www.zetetic.net/sqlcipher/sqlcipher-api/).
The LaunchAgent follows Apple's [RunAtLoad/KeepAlive model](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html).

## Implementation progress

| Phase | State | Verification |
| --- | --- | --- |
| 1 — foundation | Complete: package, models, SQLCipher schema, Keychain adapter, fake provider | 15 tests; Ruff formatting/lint and strict mypy passed |
| 2 — provider | Live authentication/account discovery added; real transaction normalization still blocked on status/reconciliation semantics | 67 tests after Financy discovery addition; no real credentials used in tests |
| 3 — synchronization | Atomic imports, overlap, aliases/deduplication, checkpoints, locks and CLI | 29 tests at phase checkpoint |
| 4 — finance logic | Manual/automatic classification, backfill and auditable multi-currency monthly totals | 38 tests at phase checkpoint |
| 5 — automation | Fake adapter, receipts/retries, English installer, foreground worker check and launchd setup | 57 tests at phase checkpoint; real server deployment blocked |
| 6 — local AI | Deferred until the real core is stable | Outside v1 |

Financy endpoints, consent flows, banking permission screens, Conductor SDK
methods, and deployment APIs will not be guessed. Netflix Conductor (the
orchestrator) is separate from the Conductor desktop app used for development.

See [the phase verification report](docs/IMPLEMENTATION_REPORT.md) for commands,
files changed, and the external contracts still needed to complete live v1.
