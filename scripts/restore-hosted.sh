#!/usr/bin/env bash
# Restore and verify on a recovery VPS in an isolated PostgreSQL container.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo 'Usage: restore-hosted.sh ARCHIVE.age AGE_IDENTITY [--deletions LEDGER.age] [--keep]' >&2
  exit 2
fi
archive=$1
identity=$2
shift 2
ledger=
keep=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --deletions) ledger=${2:?Provide the latest encrypted deletion ledger}; shift 2 ;;
    --keep) keep=true; shift ;;
    *) echo 'Unknown restore option.' >&2; exit 2 ;;
  esac
done
if [[ ! -r "$archive" || ! -r "$identity" || ( -n "$ledger" && ! -r "$ledger" ) ]]; then
  echo 'The archive, identity or deletion ledger is unavailable.' >&2
  exit 2
fi
if [[ "$keep" == true && -z "$ledger" ]]; then
  echo 'Supply the latest deletion ledger to retain a recovery database.' >&2
  exit 2
fi

container="airkeep-restore-$(openssl rand -hex 8)"
volume="$container-data"
retain=false
cleanup() {
  if [[ "$retain" == false ]]; then
    docker rm -fv "$container" >/dev/null 2>&1 || true
    docker volume rm "$volume" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
POSTGRES_PASSWORD=$(openssl rand -hex 32)
export POSTGRES_PASSWORD
docker run -d --network none --name "$container" --volume "$volume:/var/lib/postgresql" \
  --env POSTGRES_USER=airkeep_migration --env POSTGRES_DB=airkeep --env POSTGRES_PASSWORD \
  postgres:18-alpine >/dev/null
unset POSTGRES_PASSWORD
# Check the server process after the image's temporary initialization server exits.
for ((attempt=0; attempt<60; attempt++)); do
  if docker exec "$container" pg_isready -q -h 127.0.0.1 -U airkeep_migration -d airkeep; then break; fi
  sleep 1
done
docker exec "$container" pg_isready -q -h 127.0.0.1 -U airkeep_migration -d airkeep

sql=(docker exec -i "$container" psql -X -q --set=ON_ERROR_STOP=1 -U airkeep_migration -d airkeep)
"${sql[@]}" -c 'CREATE ROLE airkeep_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS' \
  -c 'REVOKE CREATE ON SCHEMA public FROM PUBLIC'
age --decrypt --identity "$identity" -- "$archive" | docker exec -i "$container" pg_restore \
  --username airkeep_migration --dbname airkeep --single-transaction --exit-on-error --no-owner --no-acl

if [[ -n "$ledger" ]]; then
  age --decrypt --identity "$identity" -- "$ledger" | "${sql[@]}" --single-transaction \
    -c 'CREATE TEMP TABLE latest_deletions (account_id text PRIMARY KEY, deleted_at double precision NOT NULL)' \
    -c 'COPY latest_deletions (account_id, deleted_at) FROM STDIN' \
    -c 'INSERT INTO public.account_deletions SELECT * FROM latest_deletions ON CONFLICT (account_id) DO NOTHING' \
    -c "UPDATE public.hosted_email_outbox SET sealed_secret=NULL,state='canceled',lease=NULL,lease_until=0
        WHERE link_hash IN (SELECT hash FROM public.hosted_account_links
                            WHERE account_id IN (SELECT account_id FROM public.account_deletions))" \
    -c 'DELETE FROM public.accounts WHERE id IN (SELECT account_id FROM public.account_deletions)'
fi

"${sql[@]}" <<'SQL'
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM public.schema_migrations WHERE name='003_deletion_ledger.sql') THEN
        RAISE EXCEPTION 'Expected hosted deletion-ledger migration';
    END IF;
    IF EXISTS (SELECT FROM public.accounts JOIN public.account_deletions ON accounts.id=account_deletions.account_id) THEN
        RAISE EXCEPTION 'A deleted account is present in the restored database';
    END IF;
    IF EXISTS (SELECT FROM pg_class AS t JOIN pg_namespace AS n ON t.relnamespace=n.oid
               WHERE n.nspname='airkeep_data' AND t.relkind='r'
                 AND (NOT t.relrowsecurity OR NOT t.relforcerowsecurity)) THEN
        RAISE EXCEPTION 'A restored tenant table lacks forced row security';
    END IF;
END $$;
-- Exercise the policy even if the backup has no observations. Roll back the probe and grants.
BEGIN;
SELECT set_config('airkeep.restore_probe', gen_random_uuid()::text, true) AS probe \gset
INSERT INTO public.accounts(id,username,hash) VALUES (:'probe', :'probe' || '@example.invalid', '!');
INSERT INTO airkeep_data.printers(tenant_id,id) VALUES (:'probe', 'restore-probe');
INSERT INTO airkeep_data.samples(tenant_id,printer_id,ams,at,json)
VALUES (:'probe', 'restore-probe', '0', 0, '{}');
GRANT USAGE ON SCHEMA airkeep_data TO airkeep_runtime;
GRANT SELECT ON airkeep_data.samples TO airkeep_runtime;
SET LOCAL ROLE airkeep_runtime;
DO $$ BEGIN
    IF EXISTS (SELECT FROM airkeep_data.samples) THEN
        RAISE EXCEPTION 'Unscoped runtime role can read restored samples';
    END IF;
    PERFORM set_config('airkeep.tenant_id', current_setting('airkeep.restore_probe'), true);
    IF (SELECT count(*) FROM airkeep_data.samples) <> 1 THEN
        RAISE EXCEPTION 'Scoped runtime role could not read exactly its probe sample';
    END IF;
END $$;
ROLLBACK;
SQL
printf 'Verified isolated restore, deletion state, and scoped runtime sample visibility.\n'
if [[ "$keep" == true ]]; then
  retain=true
  printf 'Isolated recovery container: %s\nRecovery volume: %s\n' "$container" "$volume"
fi
