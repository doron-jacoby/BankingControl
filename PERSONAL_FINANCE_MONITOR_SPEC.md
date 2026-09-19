# Personal Finance Monitor — Implementation Specification

## 1. Goal

Build a secure, local-first Python application for macOS that imports personal financial data from a selected provider, stores it in an encrypted local database, classifies transactions, and calculates useful financial summaries without double counting.

The initial provider is Financy/Open Finance. Provider-specific code must be isolated so another provider can be added or substituted later.

Netflix Conductor will schedule and orchestrate periodic execution. The finance worker and encrypted database remain on the Mac.

The system is strictly read-only: it must never initiate a payment, transfer, trade, or any other financial operation.

## 2. Guiding principles

- Python 3.12+.
- Local-first: raw financial data stays on the Mac.
- Secure by default.
- Simple, reliable v1; no unnecessary infrastructure.
- Clear boundaries between provider, synchronization, storage, classification, analytics, and orchestration.
- Idempotent imports and safe retries.
- Product-ready seams, without implementing multi-tenancy or cloud infrastructure now.
- Business logic must not depend directly on Financy or Conductor.

Do not build in v1:

- Web UI.
- Cloud database.
- Microservices.
- Kubernetes.
- User authentication or multi-tenancy.
- Full accounting system.
- Complex AI classification.
- Local MCP integration before the core application is stable.

## 3. Runtime architecture

```text
Netflix Conductor
  └─ weekly workflow
       └─ finance_sync task (contains no transaction data)
            ↓ poll
Local Conductor worker on Mac
  └─ FinanceService
       ├─ FinanceProvider (initially Financy)
       ├─ SyncService
       ├─ ClassificationService
       ├─ AnalyticsService
       └─ SQLCipher database
            └─ encryption key in macOS Keychain
```

Conductor is orchestration only. It may receive operational metadata such as execution ID, success/failure, counts, and sanitized errors. It must not receive raw transactions, merchant descriptions, account identifiers, access tokens, encryption keys, bank credentials, or full card numbers.

The same application logic must also be runnable directly through a local CLI. Conductor is an adapter around the application, not a required dependency of its business logic.

## 4. Important Conductor deployment constraint

The local Conductor worker must be running to poll and execute tasks. A weekly Conductor schedule alone cannot wake a stopped Mac or start a worker that is not running.

Install the worker as a macOS `launchd` service with `RunAtLoad` and `KeepAlive`. Conductor controls when a sync is requested; `launchd` only keeps the worker available.

If the Mac is asleep when a task is scheduled, Conductor retry/timeouts must leave enough time for the worker to reconnect. Exact retry and timeout values should be configurable.

## 5. Provider abstraction

Define a provider contract similar to:

```python
class FinanceProvider(Protocol):
    def list_accounts(self) -> list[Account]: ...

    def fetch_transactions(
        self,
        account_id: str,
        from_date: datetime,
        to_date: datetime,
    ) -> list[ProviderTransaction]: ...
```

Implement `FinancyProvider` first. External payloads must be normalized before persistence. No Financy-specific conditions may appear in storage or analytics code.

Future implementations may include `CSVProvider`, `ManualProvider`, or another Open Banking provider.

## 6. Core data model

### Account

- `internal_id`
- `user_id` (single fixed local user in v1)
- `provider`
- `provider_account_id`
- `connection_id`, if available
- `institution`
- `account_type`
- `display_name`
- `currency`
- `metadata`
- `created_at`
- `updated_at`

Never store a full card number, bank password, or unnecessary account secrets.

### Transaction

- `internal_id`
- `user_id`
- `provider`
- `provider_transaction_id`
- `account_id`
- `transaction_date`
- `value_date`, when available
- `merchant` or `counterparty`
- `normalized_merchant`, nullable
- `original_description`
- `amount` using decimal semantics, never binary float
- `currency`
- `status`
- `transaction_type`
- `category`
- `category_source`
- `classification_version`
- `is_internal_transfer`
- `is_investment`
- `is_income`
- `is_refund`
- `is_recurring`
- `created_at`
- `updated_at`
- `raw_metadata`

### SyncState

Store per provider/account:

- `last_successful_sync`
- `last_transaction_date`
- `last_sync_started_at`
- `last_sync_finished_at`
- `status`
- sanitized `error`
- bootstrap completion status

### ClassificationRule

- match type and value
- category
- flags
- priority
- enabled state
- created/updated timestamps

Manual rules must override automatic classification.

## 7. Encrypted storage and secrets

Use SQLCipher or an equivalent encrypted SQLite implementation with authenticated, well-maintained Python bindings.

Default database path:

```text
~/Library/Application Support/PersonalFinance/finance.db
```

Generate a cryptographically strong random database key during installation and store it in macOS Keychain. Store provider tokens in Keychain when possible.

Never store secrets in:

- source code
- `.env`
- repository files
- Conductor workflow input/output
- Conductor task logs
- the database directory

Encrypted backups may copy the closed database file as-is. Document how to restore it and how the Keychain secret is backed up or recovered. Never silently create an unencrypted export.

## 8. Synchronization behavior

### First sync

1. Authenticate/obtain consent with the provider.
2. Discover accounts.
3. Import all history the provider permits.
4. Normalize and upsert records.
5. Mark bootstrap complete only after a successful transaction.

### Incremental sync

For each account:

1. Read its last successful checkpoint.
2. Fetch from `last_successful_sync - overlap_days` through now.
3. Default overlap is seven days and configurable.
4. Normalize records.
5. Upsert them atomically.
6. Advance the checkpoint only after successful persistence.
7. Produce a sanitized summary.

Repeated import of the same range must not create duplicates. Prefer stable provider transaction IDs. If unavailable, create and test a deterministic deduplication key while retaining the original payload metadata.

Pending transactions may later become final or change identifiers. The overlap and matching strategy must support updates without double counting.

A provider failure must preserve existing data and must not advance the affected checkpoint.

Use a local inter-process lock so CLI and Conductor cannot run sync concurrently. If a sync is already active, a duplicate Conductor delivery should return a safe idempotent result rather than starting a second import.

## 9. Classification

Create a replaceable classifier contract:

```python
class TransactionClassifier(Protocol):
    def classify(self, transaction: Transaction) -> Classification: ...
```

Initial categories:

- `FOOD`
- `RESTAURANT`
- `ENTERTAINMENT`
- `SHOPPING`
- `TRANSPORT`
- `TRAVEL`
- `UTILITIES`
- `HEALTH`
- `EDUCATION`
- `SUBSCRIPTIONS`
- `INSURANCE`
- `TAX`
- `INCOME`
- `TRANSFER`
- `INVESTMENT`
- `CASH`
- `OTHER`

Classification and flags are derived data and must be recomputable. Implement backfill commands:

```bash
finance classify --all
finance classify --from 2026-01-01
```

Classification must remain separate from synchronization. A sync may classify newly imported records after persistence, but failure to classify must not corrupt or lose imported source data.

Resolution order:

1. Manual user rules.
2. Known merchant rules.
3. Simple heuristics.
4. Future AI classifier.
5. `OTHER`.

## 10. Analytics

Implement first:

```python
get_real_monthly_expenses(year: int, month: int) -> MonthlyExpenseSummary
```

It must avoid blindly summing all movements. Exclude or correctly net:

- transfers between the user's own accounts
- credit-card settlement payments when card transactions are imported separately
- transfers to investment accounts
- securities purchases and other investments
- technical/bookkeeping movements
- refunds and reversals
- duplicate pending/final transaction representations

Return both the total and an auditable breakdown of included/excluded counts and amounts. Do not expose raw records to Conductor.

Future analytics should be easy to add:

- expenses by category or merchant
- recurring charges
- new recurring charges
- price increases
- duplicate charges
- unusual transactions
- month/year comparisons

## 11. CLI

Provide a `finance` command with:

```bash
finance sync
finance status
finance accounts
finance transactions --days 30
finance monthly 2026-09
finance classify --all
finance classify --from 2026-01-01
finance worker
```

`finance status` should show provider, account count, last successful sync, encrypted DB availability, transaction count, worker status if available, and last sanitized error.

Human-facing local CLI output may show financial data where explicitly requested. Logs and Conductor outputs may not.

## 12. Conductor integration

Implement Conductor in a thin adapter module. Use the Conductor client compatible with the deployed Conductor version and follow the organization's existing worker conventions.

Initial task definition:

```text
finance_sync
```

Suggested task input:

```json
{
  "request_id": "workflow-or-correlation-id",
  "mode": "incremental"
}
```

Allowed task output:

```json
{
  "status": "completed",
  "accounts_processed": 3,
  "inserted": 40,
  "updated": 3,
  "started_at": "...",
  "finished_at": "..."
}
```

Do not return transaction rows, merchant names, balances, provider account IDs, secrets, or database paths.

Create a weekly workflow definition containing the sync task. Prefer Conductor's supported schedule mechanism in the deployed environment. Keep schedule configuration separate from workflow/business code.

Retry policy must distinguish:

- transient provider/network failures: retry with backoff
- authentication or consent expiration: fail clearly and require reauthorization
- validation/programming errors: non-retryable after a limited attempt
- already-running/idempotent duplicate: complete safely

Set timeouts based on the maximum bootstrap/import duration, not only normal incremental runs. Initial bootstrap may be triggered manually rather than by the weekly workflow.

## 13. Installation on macOS

Provide `install.sh` or a small Python installer that:

1. Checks macOS and Python versions.
2. Creates an isolated virtual environment.
3. Installs dependencies.
4. Creates application/data/log directories with restrictive permissions.
5. Generates and saves the encryption key in Keychain.
6. Configures provider authentication/consent.
7. Tests provider connectivity.
8. Creates and verifies the encrypted database.
9. Performs the initial import after confirmation.
10. Configures Conductor endpoint/task settings without embedding secrets.
11. Installs the local Conductor worker as a `launchd` service.
12. Starts the worker and verifies it can poll.
13. Optionally registers/deploys the workflow and schedule if credentials and permissions allow; otherwise emits exact manual instructions.
14. Prints a sanitized installation summary.

Do not assume installation has permission to modify the shared Conductor server. Keep task/workflow definition files in the repository so they can pass the organization's normal deployment process.

## 14. Logging and observability

Use structured logs with rotation. Include timestamps, execution/request ID, provider name, counts, duration, and sanitized error type.

Allowed example:

```text
provider=financy accounts=3 inserted=40 updated=3 duration_seconds=12
```

Forbidden example:

```text
merchant=Netflix amount=69.90 account=...
```

Never log request/response bodies from the provider. Conductor task logs must follow the same restriction.

## 15. Testing

Implement `FakeFinanceProvider` and tests for at least:

- provider normalization
- money/decimal handling
- stable deduplication
- repeated idempotent sync
- overlap behavior
- pending-to-final updates
- failed sync does not advance checkpoint
- partial account failure behavior
- encrypted database creation/opening and wrong-key failure
- Keychain adapter with a fake in tests
- classification and manual override priority
- classification backfill
- internal transfer exclusion
- card settlement double-count prevention
- investment exclusion
- refund/reversal handling
- monthly expense totals and audit breakdown
- local sync lock
- duplicate Conductor task delivery
- Conductor adapter never emits sensitive fields

Tests must not require real Financy, bank, Keychain, or Conductor access.

## 16. Suggested repository structure

```text
personal-finance/
├── pyproject.toml
├── README.md
├── install.sh
├── conductor/
│   ├── workflow.json
│   ├── task-definition.json
│   └── schedule.example.json
├── launchd/
│   └── com.personalfinance.worker.plist.template
├── src/finance/
│   ├── cli.py
│   ├── config.py
│   ├── service.py
│   ├── providers/
│   │   ├── base.py
│   │   └── financy.py
│   ├── models/
│   ├── storage/
│   ├── sync/
│   ├── classification/
│   ├── analytics/
│   ├── security/
│   └── orchestration/
│       └── conductor_worker.py
└── tests/
```

The implementer may simplify this structure when a smaller design remains clear and testable.

## 17. Implementation phases

### Phase 1 — Foundation

- package/repository setup
- domain models
- encrypted database
- Keychain integration
- provider interface
- fake provider
- unit tests

### Phase 2 — Provider integration

- verify the real Financy API/MCP contract before coding against assumptions
- authentication/consent
- account discovery
- transaction normalization
- initial import

### Phase 3 — Reliable synchronization

- incremental sync
- overlap
- upsert/deduplication
- checkpoints
- locking
- error handling
- CLI

### Phase 4 — Finance logic

- basic/manual-rule classification
- internal transfer and investment detection
- card settlement handling
- refunds/reversals
- real monthly expenses
- tests and an audit report

### Phase 5 — Conductor automation

- thin worker adapter
- task/workflow definitions
- retries/timeouts/idempotency
- `launchd` worker service
- installer and logs
- end-to-end test with fake provider

### Phase 6 — Future local AI integration

Only after the local system is stable:

- read-only local MCP
- service-layer tools, never raw database access
- recurring-charge and anomaly analysis
- explicit redaction and access policy

## 18. Definition of Done for v1

v1 is complete when:

- the Mac can connect to the chosen provider and discover the intended accounts
- the initial history import is stored in an encrypted local database
- subsequent imports are incremental, overlapping, idempotent, and retry-safe
- provider failure does not corrupt data or advance checkpoints
- `finance monthly YYYY-MM` calculates a reasonable real-expense total and explains exclusions
- manual classification rules and backfill work
- the local worker runs under `launchd`
- a Conductor weekly workflow can request a sync without carrying financial data
- Conductor retries or duplicate deliveries do not duplicate data or run concurrent syncs
- logs and task outputs contain no raw financial data or secrets
- provider, storage, analytics, and orchestration layers can be tested independently

## 19. Instructions to the implementation agent

Treat this document as the implementation contract. Start by inspecting the existing repository and its instructions. If it is empty, initialize the minimal Python project needed for Phase 1.

Before implementing the real provider or Conductor client, obtain their actual API/client contracts and versions. Do not invent endpoints, authentication flows, SDK methods, or workflow deployment APIs. Use fakes until the contracts are available.

Work in small, verified increments. After each phase:

1. Run formatting, typing, and tests.
2. Summarize files changed and commands run.
3. List assumptions or missing external contracts.
4. Do not expose secrets or raw financial data in output.

Choose the simplest v1 design that satisfies these requirements and preserves the stated interfaces. Do not introduce services or abstractions solely for hypothetical future scale.

