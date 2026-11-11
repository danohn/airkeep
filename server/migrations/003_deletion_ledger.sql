-- Retain only random account identifiers to suppress deleted accounts on restore.
CREATE TABLE account_deletions (
    account_id text PRIMARY KEY,
    deleted_at double precision NOT NULL
);
