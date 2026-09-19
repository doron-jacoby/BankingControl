BEGIN IMMEDIATE;

CREATE TABLE accounts (
    internal_id TEXT PRIMARY KEY NOT NULL,
    user_id TEXT NOT NULL DEFAULT 'local' CHECK (user_id = 'local'),
    provider TEXT NOT NULL,
    provider_account_id TEXT NOT NULL,
    connection_id TEXT,
    institution TEXT NOT NULL,
    account_type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (length(currency) = 3),
    metadata TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (provider, provider_account_id),
    UNIQUE (internal_id, provider)
);

CREATE TABLE transactions (
    internal_id TEXT PRIMARY KEY NOT NULL,
    user_id TEXT NOT NULL DEFAULT 'local' CHECK (user_id = 'local'),
    provider TEXT NOT NULL,
    provider_transaction_id TEXT,
    account_id TEXT NOT NULL,
    transaction_date TEXT NOT NULL,
    value_date TEXT,
    merchant TEXT,
    normalized_merchant TEXT,
    original_description TEXT NOT NULL,
    -- TEXT affinity avoids SQLite's binary-float NUMERIC conversion.
    amount DECIMAL_TEXT NOT NULL CHECK (typeof(amount) = 'text'),
    currency TEXT NOT NULL CHECK (length(currency) = 3),
    status TEXT NOT NULL CHECK (status IN ('pending', 'posted', 'reversed')),
    transaction_type TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'OTHER',
    category_source TEXT NOT NULL DEFAULT 'unclassified',
    classification_version INTEGER NOT NULL DEFAULT 0,
    is_internal_transfer INTEGER NOT NULL DEFAULT 0 CHECK (is_internal_transfer IN (0, 1)),
    is_investment INTEGER NOT NULL DEFAULT 0 CHECK (is_investment IN (0, 1)),
    is_income INTEGER NOT NULL DEFAULT 0 CHECK (is_income IN (0, 1)),
    is_refund INTEGER NOT NULL DEFAULT 0 CHECK (is_refund IN (0, 1)),
    is_recurring INTEGER NOT NULL DEFAULT 0 CHECK (is_recurring IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    raw_metadata TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(raw_metadata)),
    FOREIGN KEY (account_id, provider) REFERENCES accounts (internal_id, provider),
    UNIQUE (provider, account_id, provider_transaction_id)
);
CREATE INDEX transactions_date ON transactions (transaction_date);

-- Retain old pending IDs after a provider changes the ID on settlement.
CREATE TABLE transaction_aliases (
    account_id TEXT NOT NULL REFERENCES accounts (internal_id),
    source_key TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES transactions (internal_id),
    PRIMARY KEY (account_id, source_key)
);

CREATE TABLE sync_states (
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    last_successful_sync TEXT,
    last_transaction_date TEXT,
    last_sync_started_at TEXT,
    last_sync_finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'never'
        CHECK (status IN ('never', 'running', 'succeeded', 'failed')),
    error TEXT CHECK (error IN ('transient', 'authentication', 'validation', 'storage')),
    bootstrap_complete INTEGER NOT NULL DEFAULT 0 CHECK (bootstrap_complete IN (0, 1)),
    PRIMARY KEY (provider, account_id),
    FOREIGN KEY (account_id, provider) REFERENCES accounts (internal_id, provider)
);

CREATE TABLE classification_rules (
    internal_id TEXT PRIMARY KEY NOT NULL,
    match_type TEXT NOT NULL,
    match_value TEXT NOT NULL,
    category TEXT NOT NULL,
    flags TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(flags)),
    priority INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

PRAGMA user_version = 1;
COMMIT;
