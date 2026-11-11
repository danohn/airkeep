-- Apply with the migration role. Application connections use a separate role.
CREATE SCHEMA IF NOT EXISTS airkeep_data;
CREATE TABLE accounts (
    id text PRIMARY KEY, username text NOT NULL UNIQUE, hash text NOT NULL,
    role text NOT NULL DEFAULT 'member' CHECK (role IN ('member','admin')),
    disabled integer NOT NULL DEFAULT 0, temporary_until double precision,
    version integer NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX accounts_username_lower ON accounts (lower(username));
CREATE TABLE oauth_requests (
    id text PRIMARY KEY, client text NOT NULL, redirect text NOT NULL,
    state text NOT NULL, challenge text NOT NULL, installation text,
    expires double precision NOT NULL, "user" text REFERENCES accounts(id) ON DELETE CASCADE,
    version integer, code text UNIQUE
);
CREATE TABLE oauth_grants (
    id text PRIMARY KEY, "user" text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    client text NOT NULL, installation text, parent text REFERENCES oauth_grants(id) ON DELETE CASCADE,
    expires double precision NOT NULL, idle_until double precision NOT NULL,
    revoked integer NOT NULL DEFAULT 0
);
CREATE INDEX oauth_grants_user ON oauth_grants ("user");
CREATE INDEX oauth_grants_parent ON oauth_grants (parent);
CREATE TABLE oauth_refresh (
    hash text PRIMARY KEY, grant_id text NOT NULL REFERENCES oauth_grants(id) ON DELETE CASCADE,
    spent integer NOT NULL DEFAULT 0
);
CREATE INDEX oauth_refresh_grant ON oauth_refresh (grant_id);
CREATE TABLE oauth_attempts (bucket text NOT NULL, at double precision NOT NULL);
CREATE INDEX oauth_attempts_bucket ON oauth_attempts (bucket, at);
CREATE TABLE oauth_device_requests (
    device_hash text PRIMARY KEY, user_hash text NOT NULL UNIQUE,
    installation text NOT NULL, expires double precision NOT NULL,
    next_poll double precision NOT NULL, poll_interval integer NOT NULL,
    decision text NOT NULL DEFAULT 'pending', owner text REFERENCES accounts(id) ON DELETE CASCADE,
    owner_version integer, client text NOT NULL DEFAULT 'esp32'
);
CREATE TABLE relay_credentials (
    hash text PRIMARY KEY, "user" text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    printer_id text NOT NULL, created double precision NOT NULL,
    revoked integer NOT NULL DEFAULT 0
);
CREATE TABLE airkeep_data.printers (
    tenant_id text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    id text NOT NULL, heartbeat double precision, connected integer NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, id), UNIQUE (tenant_id)
);
CREATE TABLE airkeep_data.samples (
    tenant_id text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    printer_id text NOT NULL, ams text NOT NULL, at double precision NOT NULL, json text NOT NULL,
    PRIMARY KEY (tenant_id, printer_id, ams, at),
    FOREIGN KEY (tenant_id, printer_id) REFERENCES airkeep_data.printers(tenant_id,id) ON DELETE CASCADE
);
CREATE TABLE airkeep_data.latest (
    tenant_id text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    printer_id text NOT NULL, ams text NOT NULL, json text NOT NULL,
    PRIMARY KEY (tenant_id, printer_id, ams),
    FOREIGN KEY (tenant_id, printer_id) REFERENCES airkeep_data.printers(tenant_id,id) ON DELETE CASCADE
);
CREATE TABLE airkeep_data.ingest_events (
    tenant_id text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    printer_id text NOT NULL, id text NOT NULL, at double precision NOT NULL,
    PRIMARY KEY (tenant_id, printer_id, id),
    FOREIGN KEY (tenant_id, printer_id) REFERENCES airkeep_data.printers(tenant_id,id) ON DELETE CASCADE
);
CREATE TABLE airkeep_data.config (
    id text NOT NULL, json text NOT NULL,
    tenant_id text NOT NULL DEFAULT nullif(current_setting('airkeep.tenant_id',true),'') REFERENCES accounts(id) ON DELETE CASCADE,
    PRIMARY KEY (tenant_id,id)
);
CREATE TABLE airkeep_data.devices (
    id text NOT NULL, token text NOT NULL, topic text NOT NULL, environment text NOT NULL,
    receive_alerts integer NOT NULL DEFAULT 0, created_at double precision, registered_at double precision,
    tenant_id text NOT NULL DEFAULT nullif(current_setting('airkeep.tenant_id',true),'') REFERENCES accounts(id) ON DELETE CASCADE,
    PRIMARY KEY (tenant_id,id)
);
CREATE TABLE airkeep_data.activities (
    id text NOT NULL, token text NOT NULL, environment text NOT NULL, ams text NOT NULL,
    expires double precision NOT NULL, last_sent double precision NOT NULL DEFAULT 0,
    created_at double precision, registered_at double precision,
    tenant_id text NOT NULL DEFAULT nullif(current_setting('airkeep.tenant_id',true),'') REFERENCES accounts(id) ON DELETE CASCADE,
    PRIMARY KEY (tenant_id,id)
);
CREATE TABLE airkeep_data.alert_rules (
    id text NOT NULL, name text NOT NULL, revision integer NOT NULL,
    json text NOT NULL, state text NOT NULL, created double precision NOT NULL,
    owner_id text NOT NULL,
    tenant_id text NOT NULL DEFAULT nullif(current_setting('airkeep.tenant_id',true),'') REFERENCES accounts(id) ON DELETE CASCADE,
    PRIMARY KEY (tenant_id,id), CHECK (owner_id=tenant_id)
);
CREATE TABLE airkeep_data.alert_events (
    id text NOT NULL, at double precision NOT NULL, rule_id text NOT NULL,
    ams text NOT NULL, json text NOT NULL, owner_id text NOT NULL,
    tenant_id text NOT NULL DEFAULT nullif(current_setting('airkeep.tenant_id',true),'') REFERENCES accounts(id) ON DELETE CASCADE,
    PRIMARY KEY (tenant_id,id), CHECK (owner_id=tenant_id)
);
CREATE INDEX alert_events_owner ON airkeep_data.alert_events (tenant_id,at DESC,id DESC);
CREATE TABLE airkeep_data.outbox (
    id bigint GENERATED ALWAYS AS IDENTITY, device text NOT NULL, json text NOT NULL,
    attempts integer NOT NULL DEFAULT 0, next double precision NOT NULL DEFAULT 0,
    created double precision NOT NULL DEFAULT 0, rule_id text NOT NULL,
    tenant_id text NOT NULL DEFAULT nullif(current_setting('airkeep.tenant_id',true),'') REFERENCES accounts(id) ON DELETE CASCADE,
    PRIMARY KEY (tenant_id,id),
    FOREIGN KEY (tenant_id,device) REFERENCES airkeep_data.devices(tenant_id,id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id,rule_id) REFERENCES airkeep_data.alert_rules(tenant_id,id) ON DELETE CASCADE
);
CREATE INDEX outbox_next ON airkeep_data.outbox (tenant_id,next);
CREATE TABLE airkeep_data.registration_owners (
    kind text NOT NULL, id text NOT NULL, grant_id text NOT NULL REFERENCES oauth_grants(id) ON DELETE CASCADE,
    "user" text NOT NULL,
    tenant_id text NOT NULL DEFAULT nullif(current_setting('airkeep.tenant_id',true),'') REFERENCES accounts(id) ON DELETE CASCADE,
    PRIMARY KEY (tenant_id,kind,id), CHECK ("user"=tenant_id)
);
CREATE TABLE airkeep_data.push_attempts (
    id bigint GENERATED ALWAYS AS IDENTITY, at double precision NOT NULL,
    kind text, destination text, fingerprint text, environment text, topic text,
    status integer, reason text, apns_id text,
    tenant_id text NOT NULL DEFAULT nullif(current_setting('airkeep.tenant_id',true),'') REFERENCES accounts(id) ON DELETE CASCADE,
    PRIMARY KEY (tenant_id,id)
);
CREATE TABLE airkeep_data.widget_installations (
    id text NOT NULL, generation bigint NOT NULL, deleted integer NOT NULL DEFAULT 0,
    token text, environment text, bundle_id text, ams_ids text NOT NULL,
    created double precision, updated double precision,
    last_sent double precision NOT NULL DEFAULT 0, last_attempt double precision NOT NULL DEFAULT 0,
    last_fingerprint text, next_attempt double precision NOT NULL DEFAULT 0,
    attempts integer NOT NULL DEFAULT 0, last_status integer, last_reason text,
    tenant_id text NOT NULL DEFAULT nullif(current_setting('airkeep.tenant_id',true),'') REFERENCES accounts(id) ON DELETE CASCADE,
    PRIMARY KEY (tenant_id,id)
);
