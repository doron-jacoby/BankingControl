# Implementation and verification report

The specification was copied unchanged from Downloads into the repository.
Implementation proceeded phase by phase, extending the scope after the request
to build everything possible and provide guided installation.

## Phase checkpoints

| Phase | Main files | Result at checkpoint |
| --- | --- | --- |
| 1 | `pyproject.toml`, `models.py`, `storage.py`, `schema.sql`, `security.py`, `providers.py`, foundation tests | 15 tests passed |
| 2 (fake only) | Synthetic normalization in `providers.py`, normalization tests | 20 tests passed |
| 3 | `repository.py`, `sync.py`, `demo.py`, `cli.py`, sync/CLI tests | 29 tests passed |
| 4 | `classification.py`, `analytics.py`, `service.py`, finance tests, audit example | 38 tests passed |
| 5 (fake orchestration) | `orchestration.py`, `worker.py`, `install.py`, `install.sh`, Conductor review plans, launchd template, automation tests | 57 tests passed after final regression checks |

At every checkpoint the following commands passed:

```sh
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/python -m unittest discover -s tests -v
```

Final additional checks:

```sh
bash -n install.sh
./install.sh --check
plutil -lint launchd/com.personalfinance.worker.plist.template
git diff --check
.venv/bin/python -m pip wheel --no-deps --wheel-dir .context/dist .
```

The built wheel was also installed into a separate virtual environment and
tested outside the source checkout. This checks packaged SQL schema resources,
the installed `finance` entry point and dependencies, rather than relying only
on an editable development installation.

Environment: macOS on Apple Silicon, Python 3.14.6, sqlcipher3 0.6.2,
keyring 25.7.0. Python 3.12+ is declared; the actual local runtime tested was 3.14.

## What the tests exercise

Real SQLCipher encryption, HMAC settings, wrong-key and ordinary SQLite
rejection, exact decimal persistence, rollback, private permissions and symlink
rejection; fake-backed Keychain reads/writes and sanitized failures; normalized
synthetic records; bootstrap and overlapping incremental imports; stable IDs,
ID-less multiplicity, pending/final aliases and reversed-state protection;
failed and partial-account imports; same-process and separate-process locking;
manual rules, backfill and post-import classification failure; multi-currency
expense totals, exclusions, refunds, reversals and timezone boundaries;
duplicate task receipts across service restarts; retry policy and log/output
redaction; month refresh in long-lived demo workers; interactive installer
confirmation, reruns, one-shot worker execution and generated LaunchAgent data.

Tests use no actual Keychain, bank, provider, or Conductor credentials. The
installer end-to-end tests use a fake Keychain and stub launchctl startup. The
real LaunchAgent was not installed or started on the user's Mac by the test
suite. The guided installer performs those interactive permission/startup checks
when the user runs it.

## Remaining external contracts

Live v1 is not yet complete. The functioning end-to-end path is synthetic:

1. Financy/Open Finance: exact product, API/SDK version, official signup and
   consent documentation, authentication/revocation/renewal, pagination, history
   limits, account and transaction schemas, amount sign semantics, pending/final
   reconciliation and settlement/statement coverage.
2. Bank Leumi: the provider-approved consent flow, requested read-only scopes,
   expiration and revocation. No permission screen or bank instruction is guessed.
3. Netflix Conductor: deployed version, compatible client, endpoint,
   authentication, organization worker/deployment conventions, supported
   scheduling mechanism and production timeout requirements. JSON files in
   `conductor/` are explicitly non-deployable review plans.

The local fake uses the application's own provider and queue formats; neither
format claims to represent a real external API. No real server configuration or
workflow deployment occurred. Read the audit example for conservative financial
assumptions that need validation against real statement data.

Phase 6 (MCP and AI analysis) remains deferred, as the specification requires the
real core to be stable first. No bank operations, payments, transfers or trades
are implemented.
