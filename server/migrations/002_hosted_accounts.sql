-- Account-plane tables. The API and maintenance roles require SELECT/INSERT/UPDATE/DELETE.
CREATE TABLE IF NOT EXISTS hosted_account_links (
    hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('signup', 'reset')),
    email TEXT NOT NULL,
    account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE,
    account_version INTEGER,
    created DOUBLE PRECISION NOT NULL,
    expires DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS hosted_account_links_account ON hosted_account_links(account_id);
CREATE INDEX IF NOT EXISTS hosted_account_links_email ON hosted_account_links(email);
CREATE TABLE IF NOT EXISTS hosted_email_outbox (
    id TEXT PRIMARY KEY,
    link_hash TEXT REFERENCES hosted_account_links(hash) ON DELETE SET NULL,
    sealed_secret TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt DOUBLE PRECISION NOT NULL,
    lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
    lease TEXT,
    operation_id TEXT,
    last_status INTEGER,
    created DOUBLE PRECISION NOT NULL,
    updated DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS hosted_email_outbox_due ON hosted_email_outbox(state,next_attempt);
CREATE INDEX IF NOT EXISTS hosted_email_outbox_link ON hosted_email_outbox(link_hash);
CREATE TABLE IF NOT EXISTS hosted_account_limits (
    bucket TEXT PRIMARY KEY,
    count INTEGER NOT NULL,
    expires DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS hosted_account_limits_expires ON hosted_account_limits(expires);
