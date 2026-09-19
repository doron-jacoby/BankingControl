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
Enter User ID, Client ID, then Client secret, matching the site's order.
User ID stays visible; Client ID and Client secret appear as `*`.
Client ID must have 32 characters and Client secret
64; an incorrect length prompts you to retry that field. User ID length varies.
No credential prefixes or character counts are displayed. Credentials are
verified before being stored as one macOS Keychain item. There are no extra
Enter confirmations between instructions.

Live **provisional transaction analysis** is available through `sync`, `monthly`
and `report`. The default report is a concise Hebrew overview with twelve
completed calendar months, general categories plus Unidentified, and review items
for the latest completed month. `report --simple` remains a compatible alias;
`report --detailed` exports the English diagnostic tables.

Reports default to `~/Documents/PersonalFinance`, independently of the database
location. Each `finance report` saves both `monthly-overview.html` and
`monthly-overview.pdf`; `--detailed` saves `analysis.html` and `analysis.pdf`.
`--output /path/report.html` (or `.pdf`) overrides the destination for both files.
PDF export uses the installed Google Chrome in `/Applications`, with a temporary
browser profile and no upload. The Hebrew PDF uses landscape A4 pages.
If PDF export fails, the command reports failure and preserves the previous PDF.

All report amounts are shown in shekels, in thousands with exactly one decimal.
USD and EUR are converted to ILS using the Bank of Israel's last published daily
representative rate on or before each transaction month's end. A rate is saved
once per currency/month in the encrypted `month_end_rates` table, with observation
date, source URL and capture time; report regeneration and resync never overwrite
it. Missing rates are fetched from the [official historical API](https://www.boi.org.il/information/bank-paymnts/guide/api-guide/).
Subsequent reports work offline when the required rates have already been saved.
A missing/invalid rate stops export and preserves the previous report. Original
transaction currencies/amounts remain unchanged; conversion uses exact Decimal
arithmetic before display rounding. Diagnostics retain original currencies.

Categories are food, shopping, domestic transport, education, health/insurance,
leisure, overseas travel, Other and Unidentified. HaKfar HaYarok charges are
Education, including the user-confirmed school name found in bank descriptions;
only its canonical name is retained. תחנת החוף המנהרה charges are domestic
transport (a user-confirmed car wash), despite Financy's FOOD_&_DRINKS/RESTAURANT
category. There is no housing category. Unidentified
expenses follow Other and are included in the **total excluding overseas travel**;
travel is the next column. Unresolved non-expense movements remain separate.
Flights go to overseas travel; transport with a foreign merchant country goes
there too. Subscriptions follow their subject; an unknown subject is Unidentified.

Identified spending excludes unresolved transfers, bank card settlements,
investment movements, credits, incomplete statuses and missing amounts. These
remain visible for review. A label is a reason to withhold a movement, not proof
of a matched transfer. Explicit `expense` tags override the withholding; a
positive amount tagged as expense reduces spending (a confirmed refund).
`self_transfer`, `gift` and `income` retain their manual meanings.

Review lists include fees, insurance, unclassified charges, unresolved movements,
and missing amounts/statuses. Increases above 125% of the median of at least two
prior charges are flagged within the same account, currency, category,
subcategory and merchant (when supplied). Same-month outliers above 3x median
are also flagged when at least three comparable debits exist. Insurance remains
visible without a history baseline. Expense classification does not acknowledge
future charges. These are review hints, not proof of an error.

Missing and partial months are labelled explicitly; requested dates alone do not
prove complete provider history. The latest completed month is included when the
snapshot ends on its last day. Identifiers are available in collapsed review
rows so users can trace and tag a movement. All exports remain private local HTML/PDF.

The default report also includes a fees/insurance/subscriptions/standing-orders
section: a twelve-month trend of each group's total, plus the ten most expensive
items of each group in the latest completed month, grouped by merchant and sorted
most expensive first, with charge dates and account. Fees come from fee keywords
in the category labels or Hebrew fee words in the merchant name (עמלה, דמי כרטיס,
דמי ניהול); Financy's `INSURANCE_&_FEES` premiums are listed as insurance, not
fees. Financy has no subscription label, so subscriptions are estimated: a
merchant charged about once a month at a stable price (within 20% of its median)
in at least three months. Standing orders are the `DIRECT_DEBIT` subcategory;
Financy gives no payee name, so they are identified by date and account. Amounts
here are in full shekels, not thousands. Self-transfers, gifts and income are
excluded, as in the rest of the report.

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
"$finance" sync                # Twelve prior calendar months plus this month to date
"$finance" sync --from 2026-06-01 --to 2026-09-19
"$finance" monthly 2026-08      # Provisional live movements/category breakdown
"$finance" report              # Hebrew HTML + PDF in ~/Documents/PersonalFinance
"$finance" report --simple     # Same Hebrew overview (compatibility alias)
"$finance" report --detailed   # English diagnostic analysis.html + analysis.pdf there
"$finance" tag self_transfer --account-id ACC --record-id TX  # One specific record
"$finance" tag gift --category TRANSFER --subcategory PRIVATE # A recurring pattern
"$finance" tags                # List saved reconciliation rules in priority order
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

Each live sync atomically replaces the previous snapshot with the requested date
window. `monthly` reads it offline; `report` additionally fetches any missing
month-end exchange rates, then reuses the saved rates offline. The HTML and PDF reports contain plaintext
aggregates and are written with mode 0600. The HTML loads no external assets; the database
remains encrypted. Identified purchases can be combined across bank and card accounts, but unresolved
settlements/transfers are withheld. Source statuses other than `BOOKED` remain
outside spending totals. Credits are not assumed to be
refunds. Missing charged amounts are counted for review and omitted from monetary
sums; original amounts are not substituted. Dates prefer transaction date, then
booking date, then value date. Source dates have no time, so live timezone
conversion is unavailable. Complete requested months do not prove complete bank
history. The live worker remains disabled.

Financy exposes no counterparty details, so movements between the user's own
accounts, gifts from other people, and income landing in an unexpected category
(such as ESOP sale proceeds credited to a securities account) cannot be inferred
from amounts or provider labels. `finance tag` records the user's own
determination as a rule: a one-off override for a specific `--account-id`
plus `--record-id`, or a reusable pattern by `--category`/`--subcategory`
(optionally scoped to one `--account-id`). Rules live in the encrypted database
next to the snapshot, survive `sync` replacing the snapshot, and are reapplied
every time `monthly` or `report` runs, so adding or editing a rule updates
historical months without re-entering each transaction. Higher `--priority`
rules are checked first; the first match wins.

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
| 2 — provider | Live discovery, encrypted provisional snapshots, user-tagged reconciled spend/income, flagged-for-review debits and a short Hebrew monthly overview; full expense normalization still awaits status/settlement semantics | Regression suite and separate authorized live import/report check |
| 3 — synchronization | Atomic imports, overlap, aliases/deduplication, checkpoints, locks and CLI | 29 tests at phase checkpoint |
| 4 — finance logic | Manual/automatic classification, backfill and auditable multi-currency monthly totals | 38 tests at phase checkpoint |
| 5 — automation | Fake adapter, receipts/retries, English installer, foreground worker check and launchd setup | 57 tests at phase checkpoint; real server deployment blocked |
| 6 — local AI | Deferred until the real core is stable | Outside v1 |

Financy endpoints, consent flows, banking permission screens, Conductor SDK
methods, and deployment APIs will not be guessed. Netflix Conductor (the
orchestrator) is separate from the Conductor desktop app used for development.

See [the phase verification report](docs/IMPLEMENTATION_REPORT.md) for commands,
files changed, and the external contracts still needed to complete live v1.
