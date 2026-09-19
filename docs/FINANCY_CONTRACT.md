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

Confirm those details with Financy before enabling transaction normalization.
The demo remains available for testing the complete local pipeline. No live
credential or bank-data request was made while implementing these changes;
tests stub HTTPS and Keychain.
