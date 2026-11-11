# Hosted Airkeep deployment

Airkeep's hosted stack runs on a VPS. Caddy serves HTTPS, the web container serves the dashboard, and separate API and worker containers use PostgreSQL. The LAN agent collects printer data at the printer's site. Printer IP addresses, serials and LAN access codes stay with the agent. Use [the LAN agent guide](../agent/README.md) for setup.

## Start a closed deployment

Use a reviewed revision on the VPS with Docker Engine, Compose v2, `uv`, `age`, OpenSSL and enough storage for PostgreSQL and local encrypted backups. Point the dashboard hostname at the VPS and allow inbound TCP 80 and 443. UDP 443 enables HTTP/3. Compose publishes only ingress ports. Run a separate staging VPS or use an isolated staging ingress, DNS name and database.

```bash
uv run --no-project --python 3.14 python scripts/setup-hosted.py \
  --origin https://dashboard.airkeep.io

dc=(docker compose --env-file server/.env.hosted -f server/compose.hosted.yml)
"${dc[@]}" config --quiet
"${dc[@]}" build migrate web
"${dc[@]}" up -d --wait postgres
"${dc[@]}" run --rm --no-deps migrate
"${dc[@]}" up -d --wait api worker web ingress
curl -fsS https://dashboard.airkeep.io/health
curl -fsS https://dashboard.airkeep.io/auth/session
"${dc[@]}" exec -T api python hosted_admin.py health
```

The setup command creates `server/.env.hosted` with mode `0600` and refuses to overwrite it. Keep the generated migration password, runtime password, JWT signing key and email-link key in the operator's secret store. Keep the database and APNs files private and keep Compose output containing expanded settings out of logs and support reports. The migration service uses the database owner role. API and worker use `airkeep_runtime`, a restricted role that does not own the tables and has no `BYPASSRLS`. Changing a password in the env file alone does not change a role in an initialized PostgreSQL volume.

For a build from source, keep the local `AIRKEEP_IMAGE_TAG` for that revision. For a release from the container registry, set the env file to the tested immutable tag, record both image digests, and use `"${dc[@]}" pull migrate web` in place of `build migrate web`. Retain the previous revision and image digests until verification finishes. [Docker Compose configuration](https://docs.docker.com/reference/cli/docker/compose/config/)

## Email, APNs and signup

Set the following values in the private env file. Register and authenticate the sender domain with Twilio Email before using an address from that domain. Airkeep posts through the native Twilio Email API at `https://comms.twilio.com/v1/Emails` and uses the API key SID and secret for Basic authentication. The worker tracks operation status after Twilio accepts a submission. Verify an actual delivery to a controlled staging mailbox. [Twilio Mail Send](https://www.twilio.com/docs/email/api/mail-send), [Twilio operation tracking](https://www.twilio.com/docs/email/api/operations-and-email-tracking)

| Setting | Value |
| --- | --- |
| `AIRKEEP_TWILIO_API_KEY` | Twilio API key SID for the Email account |
| `AIRKEEP_TWILIO_API_SECRET` | Secret for that Twilio API key |
| `AIRKEEP_EMAIL_FROM` | Address on the verified sender domain |
| `AIRKEEP_EMAIL_FROM_NAME` | Sender display name, normally `Airkeep` |
| `AIRKEEP_EMAIL_LINK_KEY` | Generated Fernet key used to encrypt queued one-time links |
| `AIRKEEP_PUBLIC_SIGNUP` | Keep `false` during deployment and release checks |

Keep `.p8` push keys under `server/private/apns`, mounted read-only as `/run/keys`. Give the container's GID `10001` read access and keep other host users out. Set `AIRKEEP_APNS_TEAM_ID`, `AIRKEEP_APNS_PRODUCTION_KEY_ID` and `AIRKEEP_APNS_PRODUCTION_KEY_PATH=/run/keys/<filename>.p8` for production. The sandbox key uses `AIRKEEP_APNS_KEY_ID` and `AIRKEEP_APNS_KEY_PATH`. For Watch use the matching `AIRKEEP_WATCH_APNS_PRODUCTION_KEY_ID` and `AIRKEEP_WATCH_APNS_PRODUCTION_KEY_PATH`, with `AIRKEEP_WATCH_APNS_KEY_ID` and `AIRKEEP_WATCH_APNS_KEY_PATH` for sandbox. Set `AIRKEEP_WATCH_APNS_USE_SHARED_CREDENTIALS=true` only when the shared key covers the Watch topics. Confirm `AIRKEEP_IOS_TOPIC`, `AIRKEEP_MAC_TOPIC` and `AIRKEEP_WATCH_TOPIC` match the signed bundle IDs. Test iOS Live Activities and Watch widgets as well as alerts on iPhone, Watch and Mac. [Apple APNs token configuration](https://developer.apple.com/documentation/usernotifications/establishing-a-token-based-connection-to-apns)

Recreate API and worker after editing settings. Use this command to close signup as well. Set `AIRKEEP_PUBLIC_SIGNUP=false` in the env file first, then verify `/auth/session` reports `public_signup: false`.

```bash
"${dc[@]}" up -d --wait --force-recreate api worker
```

Before enabling production signup for the web release, verify the following on staging with controlled accounts and email.

1. Temporarily set `AIRKEEP_PUBLIC_SIGNUP=true` on staging. Complete email verification, sign-in and password reset. Check a reused or expired link fails, an unknown address gives the same reset response, and a reset revokes existing app and relay access. Close staging signup after testing.
2. Enroll a LAN agent and verify the live snapshot and replayed history. Confirm two accounts with the same AMS ID have separate readings and rules. Verify hosted Admin access, tenant selection, suspension, reactivation, invitations, and email resets.
3. Delete a disposable account from `/account`. Confirm sign-in and all prior grants fail, `python hosted_admin.py deletion-ledger` in the API container shows the deletion entry, and an isolated restore of a backup from before deletion removes the account and its data when supplied the latest ledger.
4. Verify encrypted backups, 30-day expiration, and a completed isolated restore. Confirm the public privacy notice and support contact match the live account and data deletion paths.

Enable production signup by setting `AIRKEEP_PUBLIC_SIGNUP=true`, recreating API and worker, and checking `/auth/session` plus a controlled production verification message. Keep the env flag off when the checks above are still pending.

Verify signed Apple builds, push delivery, Watch pairing, and client account switching for the Apple release. Verify ESP32 pairing on hardware for the display release. Off-host backup delivery is deferred for this web release. The on-server backup covers restore drills and database recovery while the VPS and its backup disk are available.

## Cloud administrators

Grant or remove a hosted administrator role by canonical email from the API container.

```bash
"${dc[@]}" exec -T api python hosted_admin.py grant-admin operator@example.com
"${dc[@]}" exec -T api python hosted_admin.py revoke-admin operator@example.com
```

The account must already exist and be active. Role changes preserve ordinary client and relay grants. The Admin dashboard can view service health and selected-account diagnostics, suspend and reactivate member accounts, and queue invitations or password-reset emails. Audit events appear in the API container logs. Preserve the output of host role changes in the operator log.

## Verification pipeline

Run the repository checks on a development machine. Run container builds and the live PostgreSQL tests on the VPS or CI. Use an empty, migrated test database with the restricted runtime role, separate from the deployed database. Supply its private DSN through `AIRKEEP_TEST_DATABASE_URL` from the test runner's secret environment. The hosted tests report a skip when that DSN is absent.

```bash
scripts/check.sh
uv run --locked --directory agent pytest -q
npm ci --prefix site && npm test --prefix site
uv run --no-project --python 3.14 python scripts/test-apple-release.py
# On the isolated PostgreSQL test runner, with AIRKEEP_TEST_DATABASE_URL set
uv run --locked --directory server pytest -q tests/test_hosted.py tests/test_hosted_accounts.py
```

Check the **Container images**, **LAN agent image**, **ESP32 firmware**, and **Apple signed release** workflows for their corresponding build and release gates. A passing simulator build does not exercise signed device installation, production APNs, or ESP32 hardware pairing.

## Encrypted backup and deletion ledger

Generate an `age` identity on the recovery system and save it in private recovery storage. Keep only the public recipient on the VPS. Install an SSH key for an operator-managed off-host backup destination, pre-verify its host key, and set a dedicated destination directory with private access. This SSH key grants transfer access and is separate from the age identity. [age key management](https://github.com/FiloSottile/age)

```bash
# On the recovery system
umask 077
age-keygen -o /path/to/private/airkeep-backup.agekey
age-keygen -y /path/to/private/airkeep-backup.agekey

# On the VPS, from the checkout
export AIRKEEP_BACKUP_RECIPIENT=age1YOUR_PUBLIC_RECIPIENT
export AIRKEEP_BACKUP_DIR=/path/to/private/airkeep-backups
export AIRKEEP_BACKUP_SSH_DESTINATION=backup-user@backup-host:/private/airkeep-backups
scripts/backup-hosted.sh
scripts/backup-hosted.sh --deletions-only
```

Set `AIRKEEP_HOSTED_ENV` if the Compose env file is elsewhere, and `AIRKEEP_BACKUP_PROJECT` if the Compose project name differs. Omitting `AIRKEEP_BACKUP_SSH_DESTINATION` writes a local encrypted backup. This supports setup and restore drills before off-host credentials are available. Production recovery depends on an independently stored copy of the dump, deletion ledger and configuration secrets.

The full backup streams a transaction-consistent PostgreSQL custom archive directly into `age`. It writes a separate encrypted ledger after the dump so deletions committed during the dump can be replayed. With SSH configured, each file is transferred under a temporary name and renamed after upload. The ledger is published before its matching dump. The database archive includes account and tenant data. PostgreSQL role passwords and server keys are kept separately. [PostgreSQL logical dumps](https://www.postgresql.org/docs/18/app-pgdump.html)

Schedule a full backup daily and a deletion-only export at least every five minutes. Alert on failed jobs and verify the latest files exist on the destination. Apply a 30-day expiration to both local and off-host files, including partial uploads, copies and storage versions. For a dedicated Linux backup directory, schedule the following hourly on each host. Ensure any storage snapshots or object-version policies expire by the same deadline.

```bash
find "$AIRKEEP_BACKUP_DIR" -maxdepth 1 -type f \
  \( -name 'airkeep-*.pgdump.age' -o -name 'airkeep-*.deletions.age' -o -name '.airkeep-*.partial' \) \
  -mmin +43199 -delete
```

Account deletion removes live records and commits an account ID and deletion time to `account_deletions` in the same transaction. The separate encrypted export preserves these tombstones across a database rollback. The worker retains live ledger entries for 31 days, which covers the 30-day archive window. Keep off-host ledger coverage for every unexpired database backup. If the source is available, stop API and worker writes and export the ledger again before recovery. If it is unavailable, reconcile the newest off-host ledger against the recovery point and incident timeline. Keep the recovered service private until deletion coverage is established. An older encrypted database dump can contain deleted data until it expires.

## Isolated restore and recovery

On the recovery VPS, use the encrypted dump, the latest verified ledger and the private identity. The restore script starts a fresh PostgreSQL 18 container with networking disabled and a temporary volume. It replays the supplied ledger, verifies that deleted accounts are absent, checks forced row security, and verifies scoped and unscoped runtime reads using a sample that is rolled back after the check. It removes the temporary container and volume at the end. There is no API, worker, ingress or external notification delivery in the drill. [PostgreSQL restore options](https://www.postgresql.org/docs/18/app-pgrestore.html)

```bash
scripts/restore-hosted.sh "$archive" "$age_identity" --deletions "$latest_ledger"
```

Use `--keep` with the latest ledger to retain a verified isolated database for recovery. Its printed container name can be assigned to `recovery_container`. Leave it on the isolated network. Create a fresh Compose project and new secrets for the destination. Transfer the verified data only into the empty destination database, then apply migrations to recreate restricted runtime permissions.

```bash
uv run --no-project --python 3.14 python scripts/setup-hosted.py \
  --output server/.env.hosted.recovery --origin https://dashboard.airkeep.io
rc=(docker compose --env-file server/.env.hosted.recovery -f server/compose.hosted.yml -p airkeep-recovery)
"${rc[@]}" up -d --wait postgres
test "$("${rc[@]}" exec -T postgres psql -XqAt -U airkeep_migration -d airkeep \
  -c "SELECT to_regclass('public.accounts') IS NULL")" = t
set -o pipefail
docker exec "$recovery_container" pg_dump -U airkeep_migration -d airkeep -Fc --no-owner --no-acl \
  | "${rc[@]}" exec -T postgres pg_restore -U airkeep_migration -d airkeep \
    --single-transaction --exit-on-error --no-owner --no-acl
"${rc[@]}" run --rm --no-deps migrate
```

Before starting application containers, invalidate restored sessions, relay enrollments, password hashes and delivery queues. This prevents a backup from reactivating older credentials or retrying stale deliveries. Users will reset passwords through verified email and enroll their clients again. Configure the recovered service's sender and push keys from the operator secret store, keep signup closed, and test it through restricted staging access before switching ingress.

```bash
"${rc[@]}" exec -T postgres psql -X --set=ON_ERROR_STOP=1 -U airkeep_migration -d airkeep <<'SQL'
BEGIN;
UPDATE public.accounts SET hash='!reset-required',version=version+1,role='member';
UPDATE public.oauth_grants SET revoked=1;
UPDATE public.relay_credentials SET revoked=1;
DELETE FROM public.oauth_requests;
DELETE FROM public.oauth_device_requests;
UPDATE public.hosted_email_outbox SET sealed_secret=NULL,state='canceled',lease=NULL,lease_until=0;
DELETE FROM public.hosted_account_links;
DELETE FROM airkeep_data.outbox;
DELETE FROM airkeep_data.registration_owners;
DELETE FROM airkeep_data.devices;
DELETE FROM airkeep_data.activities;
DELETE FROM airkeep_data.widget_installations;
COMMIT;
SQL
```

After recovery, regrant cloud administrator roles from the current operator list once the intended accounts have recovered access.

After verification and cutover, remove the temporary recovery container and its named volume. Record the backup date, latest ledger date, and restore check results without recording account IDs or credentials.

## Update or roll back

1. Record the running revision and image digests. Take and verify a fresh encrypted backup before applying a migration. Close signup and stop API and worker writes during schema changes.
2. Deploy the reviewed release revision. Build or pull its exact images and run the migration command. Migrations are recorded in `schema_migrations` and run under a transaction-level lock. Restart the services only after migration succeeds. Verify `/health`, `/auth/session`, worker health, the runtime operator health command and the staging test account.
3. If a rollout fails before a schema change, or the new schema is compatible with the old release, restore the previous images and configuration and verify with signup closed. If schema compatibility is uncertain, keep ingress closed and recover the pre-change encrypted backup and newest deletion ledger into a fresh project. Preserve the old database volume until the recovery is verified. Avoid resetting or replacing that volume as part of a routine image rollback.
