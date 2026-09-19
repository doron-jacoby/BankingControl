# Verified Financy contract

Checked on 2026-09-19 against Financy's official documentation. The reference
documents embed OpenAPI 3.0.0 with `info.version` **1.0.0**. The data paths use
`/v2`; the public API host is `https://api.open-finance.ai`.

Implemented:

- [Token exchange](https://docs-financy.open-finance.ai/reference/createtoken):
  `POST /oauth/token` with clientId, clientSecret and userId. Access tokens remain
  in memory and are renewed before expiry or once after a rejected read.
- [Account discovery](https://docs-financy.open-finance.ai/reference/getaccounts):
  `GET /v2/data/accounts`, using its page cursor and excluding duplicate links.
  Only required account attributes are normalized; numbers, card details,
  owner information and potentially sensitive account labels are discarded.
- [Connection readiness](https://docs-financy.open-finance.ai/docs/connections):
  `GET /v2/connections`, reduced to counts for readable/attention-needed links.
- [Transaction reads](https://docs-financy.open-finance.ai/reference/gettransactions):
  `GET /v2/data/transactions`, date-bounded with cursors, used only for provisional
  source-labelled movement reports. Date filters are not combined with `limit`.

The transport permits only those operations, uses verified HTTPS to a fixed
host, refuses redirects, bounds response size, and sanitizes errors. It cannot
call payments, trades, connection deletion or paid on-demand refresh.

Credentials are entered interactively from Settings -> API and stored in the
`PersonalFinance` Keychain service under `financy-credentials-v1`. They are never
written to configuration files, environment variables or logs. The standalone
Financy CLI is not required; its file-based credential storage is not used.

## User setup

1. Sign in at [Financy](https://financy.open-finance.ai).
2. Confirm your plan includes data API access. The
   [authentication guide](https://docs-financy.open-finance.ai/docs/authentication)
   documents paid-plan access and the Settings -> API credential screen.
3. Add the intended bank/card through Financy and complete its hosted bank
   consent journey. Connections cannot be created through this API. Follow the
   actual bank screens rather than guessed Bank Leumi button names.
4. Run `./install.sh`, select option 2, and enter credentials only into its
   local prompts. Alternatively run the installed `finance connect` command.
5. Use `finance accounts` and `finance status` for local account discovery and
   readiness checks. This setup does not initialize a live transaction database
   or start a live worker.

The user confirmed that the `clientId`, `clientSecret` and `userId` fields are
at the bottom of Settings. Use each field's copy button for the full value.
An API availability badge only indicates plan access.
The installer follows the site's order: User ID, Client ID, Client secret.
User ID is visible while typing or pasting. Client ID and Client secret appear
as `*`; no credential prefixes or counts are printed.
The user-confirmed field lengths are checked locally: 32 for Client ID, 64 for
Client secret, and no fixed length for User ID. Full credentials are verified
before being stored in macOS Keychain.

The token reference describes `expiresIn` in milliseconds, while the guide
shows `86400` without specifying units. The client uses milliseconds and caps
its in-memory cache at one day. If a deployment returns seconds, it renews
early; a rejected token also triggers one renewal. A millisecond lifetime of
`86400000` previously exceeded the client's seconds-based upper bound and
caused a generic validation error. This is fixed and covered by regression
tests. Diagnostics now include the endpoint and HTTP status for HTTP failures,
or the unexpected response field/shape, never raw response bodies or tokens.

## Transaction import gaps

The [transaction reference](https://docs-financy.open-finance.ai/reference/gettransactions)
documents charged amounts, account IDs, dates and pagination, but `status` is
only a string with no enumerated meanings. It does not define a pending-to-final
transaction link or complete settlement/reversal reconciliation semantics.
The provider's public CLI also passes these records through without resolving
those meanings. We do not infer them from undocumented status strings.

There is also a date-filter discrepancy: the guide demonstrates dates with a
page limit, while the reference says not to combine those parameters. A future
importer must resolve this or use the stricter documented combination.

Confirm those details with Financy before enabling reconciled expense normalization.
The demo remains available for testing the reconciled local pipeline.

## Provisional live analysis (2026-09-19)

An authorized live read confirmed that the response can differ from examples:
statuses include `BOOKED`, `PENDING` and missing values; `type` contains account
kinds, rather than the documented NORMAL/INSTALLMENT examples. Neither is mapped
to final ledger semantics. Some charged amounts are blank strings. Missing amounts
are retained as unavailable, counted for review and excluded from monetary sums.
Finite numeric strings are parsed exactly as Decimal when present.

`finance sync` creates or reuses the live SQLCipher database and Keychain key.
It stores one minimized snapshot in an additive `live_snapshot` table, separate
from the normalized `transactions` ledger. Optional merchant name (bounded, long digit sequences redacted) and merchant
country are retained for identifying review items and overseas transport.
With the user's approval, the documented `creditorName` is also retained as
`recipient_name` (whitespace normalized, at most 160 characters, long digit
sequences redacted). It is displayed in local reports and used as the merchant
fallback when no merchant name is supplied. Missing/null names remain empty;
non-string names fail validation without replacing the previous snapshot.
Resync the desired date window to populate names in existing history. Old
snapshots without this field remain readable. Names never enter sync metadata.
No transaction descriptions, account numbers, street addresses, whole API payloads
or credentials are copied into it.
Each successful sync atomically replaces the snapshot; failed validation leaves
the old snapshot intact. This avoids accumulating obsolete pending IDs without
claiming a verified pending/final reconciliation algorithm. It also means that
changing the requested window replaces, rather than extends, local history.

The OpenAPI `isDuplicate` description warns that duplicate records may be returned
for result sets over 500 despite the query flag. The importer therefore checks
the flag locally, removes identical repeated IDs, and rejects conflicting IDs.
Dates prefer transactionDate, bookingDate, then valueDate; selected-date outliers
are counted and excluded. Requested ranges do not prove provider history coverage.

Monthly reports keep account types, currencies and source statuses separate.
Only source-labelled BOOKED rows enter the category breakdown. Provider category
overrides take precedence; settlement, transfer and return labels are counted for
review. Untagged settlement/transfer/investment movements are withheld from
identified spending until resolved; this is not a claim of a verified match.
`finance report` writes a Hebrew HTML report with mode 0600; missing public
month-end exchange rates are fetched once, then report generation works offline;
`--detailed` selects the English diagnostic tables.
No live worker, automatic bank refresh or financial write operation is enabled.

## User-tagged reconciliation (2026-09-19)

Financy's transaction reads may include recipient names, but movements between
the user's own accounts, money received from another person, and income posted
under an unexpected category (for example ESOP sale proceeds landing in a
securities account) cannot be inferred from names, amounts or provider labels alone.
Inventing an exclusion from equal amounts or a category name would misclassify
unrelated movements, so none is attempted.

`finance tag <self_transfer|gift|income|expense>` instead records the user's own
determination as a rule in an additive `live_tags` table, alongside `live_snapshot`
in the same encrypted database. A rule matches on the specific `--account-id` plus
`--record-id` (a one-off override, requiring both), or on `--category`, optionally
narrowed by `--subcategory` and/or `--account-id` (a reusable pattern for a class
of movements the user has identified). At least one matcher is required; higher
`--priority` rules are evaluated first, and the first match wins. A local `--note`
records why, but is never sent anywhere.

Rules are independent of the snapshot: a full-window `sync` replaces
`live_snapshot` but never touches `live_tags`, so saved rules keep applying to
future syncs and are recomputed from scratch every time `monthly` or `report`
runs, exactly like `finance --demo classify` reapplies classification rules to
the normalized ledger. Only BOOKED records with a known amount are reconciled;
`self_transfer` and `gift` are excluded from both spend and income, `income`
counts toward income regardless of category, and an untagged credit is excluded
but kept visible as `unresolved_credit` rather than being assumed to be a
transfer, a refund or zeroed. `finance tags` lists saved rules for review.

`expense` tags on positive amounts represent confirmed expense adjustments and
reduce spending; untagged credits remain unresolved. All reports use the same
inclusion policy. Expense classification never acknowledges future review items.

The default report (also available as `report --simple`) shows the twelve most
recent completed calendar months, with missing/partial coverage explicit,
general categories plus Unidentified and the latest completed month's review items. A month-end
snapshot includes that completed month. Default sync requests twelve previous
calendar months plus the current partial month. ILS is displayed in thousands
with one decimal. USD/EUR amounts are converted into the same category cells
using saved Bank of Israel month-end rates; calculations retain exact Decimal
amounts. The bold total excludes overseas travel, followed by the travel column.
Unidentified expenses are after Other and included in that total. Education
replaces housing; the user-confirmed HaKfar HaYarok name can be recognized in
source descriptions without retaining those raw descriptions. Flights and transport with a known foreign merchant country go under
overseas travel. Subscription categories follow their subject. Whole-token
matching avoids classifying HEALTHCARE as CAR transport.

Review includes insurance and unknown charges without needing an outlier test,
fees, incomplete amounts/statuses, and unresolved movements. History comparisons
use account, currency, category, subcategory and merchant, where available; after
two prior charges an increase above 125% of their median is flagged. Same-month
outliers require at least three charges and exceed 3x median. No merchant means
a coarser account/subcategory comparison, not a verified recurring-payment link.
Report rows include escaped source identifiers in collapsed details for tracing
and local correction. Exact monetary values are available in tooltips.

## Fees, insurance, subscriptions and standing orders (2026-09-19)

The default report adds a section with four groups. Fees: category/subcategory
tokens in `FEE_KEYWORDS`, or Hebrew fee words in the merchant name, excluding
insurance. The live data showed Financy files insurance premiums under
`HOUSEHOLD_&_SERVICES / INSURANCE_&_FEES`, so an `INSURANCE` token makes a record
insurance, not a fee. Subscriptions: live data has no `SUBSCRIPTIONS` category, so
`recurring_merchants` estimates them from the twelve-month window in original
currencies: a named merchant with included debits in 3+ months, at most 1.5
charges per active month, and 80%+ of charges within 20% of the median amount.
Fees, insurance and standing orders are excluded from that group. Standing
orders: subcategory `DIRECT_DEBIT`; rows show the merchant or recipient name
when available, charge dates and account label.

Each group gets a twelve-month ILS total trend (`_predicate_monthly_totals`) and
the ten most expensive merchants in the latest completed month
(`expense_subjects`), sorted most expensive first. Only BOOKED debits count;
`self_transfer`, `gift` and `income` tags are excluded. Missing and partial
coverage are shown the same way as the review section. Amounts are in full
shekels rather than thousands, since most of these charges are under 1,000 ILS.

Automated tests use synthetic payloads and fake Keychain/HTTPS. A separate,
user-authorized live import/report check uses the existing Keychain credentials;
only operational counts are emitted to the development session.

## Frozen month-end exchange rates

The official [Bank of Israel series API guide](https://www.boi.org.il/information/bank-paymnts/guide/api-guide/)
documents the EXR dataflow and RER_USD_ILS/RER_EUR_ILS daily representative rates.
The importer requests one completed month, validates the currency pair, daily
frequency, representative rate type and units, then selects the last dated
observation (including the preceding business day when month-end has no quote).
No current rate or monthly average is used. The encrypted month_end_rates table
stores the exact rate, observation date, source URL and capture time once; later
imports never update an existing currency/month. Missing rates prevent replacing
the report. Original transaction values and diagnostic outputs remain in source
currencies. Price anomaly detection also uses source currencies so exchange-rate
changes cannot masquerade as merchant price increases.
