CREATE TABLE IF NOT EXISTS account_passkeys (
    credential_id text PRIMARY KEY,
    account_id text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    label text NOT NULL,
    public_key text NOT NULL,
    sign_count bigint NOT NULL,
    created_at double precision NOT NULL,
    last_used_at double precision
);
CREATE INDEX IF NOT EXISTS account_passkeys_owner ON account_passkeys(account_id);

CREATE TABLE IF NOT EXISTS account_totp (
    account_id text PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    sealed_secret text NOT NULL,
    last_step bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS account_recovery_codes (
    account_id text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    code_hash text NOT NULL,
    PRIMARY KEY (account_id, code_hash)
);

CREATE TABLE IF NOT EXISTS account_auth_challenges (
    purpose text NOT NULL,
    binding text NOT NULL,
    value text NOT NULL,
    account_id text REFERENCES accounts(id) ON DELETE CASCADE,
    version integer,
    expires double precision NOT NULL,
    PRIMARY KEY (purpose, binding)
);
CREATE INDEX IF NOT EXISTS account_auth_challenges_expiry ON account_auth_challenges(expires);
