#!/usr/bin/env bash
# Run on the VPS with access to the hosted PostgreSQL service.
set -euo pipefail

ledger_only=false
if [[ ${1:-} == --deletions-only ]]; then
  ledger_only=true
  shift
fi
if [[ $# != 0 ]]; then
  echo 'Usage: backup-hosted.sh [--deletions-only]' >&2
  exit 2
fi
: "${AIRKEEP_BACKUP_RECIPIENT:?Set the public age recipient}"
repo_root=$(cd "$(dirname "$0")/.." && pwd)
env_file=${AIRKEEP_HOSTED_ENV:-$repo_root/server/.env.hosted}
backup_dir=${AIRKEEP_BACKUP_DIR:-$repo_root/.local/hosted-backups}
destination=${AIRKEEP_BACKUP_SSH_DESTINATION:-}
if [[ -n "$destination" && ! "$destination" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]*@[a-zA-Z0-9][a-zA-Z0-9.-]*:/[a-zA-Z0-9_./-]+$ ]]; then
  echo 'Use user@host:/absolute/directory for AIRKEEP_BACKUP_SSH_DESTINATION.' >&2
  exit 2
fi
if [[ ! -r "$env_file" ]]; then
  echo 'The hosted Compose environment file is unavailable.' >&2
  exit 2
fi
compose=(docker compose --env-file "$env_file" -f "$repo_root/server/compose.hosted.yml")
if [[ -n ${AIRKEEP_BACKUP_PROJECT:-} ]]; then compose+=(-p "$AIRKEEP_BACKUP_PROJECT"); fi

umask 077
mkdir -p "$backup_dir"
working=$(mktemp -d "$backup_dir/.partial.XXXXXXXX")
trap 'rm -rf "$working"' EXIT
stamp=$(date -u +%Y%m%dT%H%M%SZ)
name="airkeep-$stamp-${working##*.}"
archive="$name.pgdump.age"
ledger="$name.deletions.age"
if [[ "$ledger_only" == false ]]; then
  # Container variables expand inside the database container.
  # shellcheck disable=SC2016
  "${compose[@]}" exec -T postgres sh -ec '
    exec pg_dump --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --format=custom --no-owner --no-acl
  ' | age --recipient "$AIRKEEP_BACKUP_RECIPIENT" --output "$working/$archive"
fi
# Export after the snapshot so this sidecar covers deletions committed while pg_dump ran.
# shellcheck disable=SC2016
"${compose[@]}" exec -T postgres sh -ec '
  exec psql -X -q --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --command "COPY (SELECT account_id, deleted_at FROM public.account_deletions ORDER BY deleted_at, account_id) TO STDOUT"
' | age --recipient "$AIRKEEP_BACKUP_RECIPIENT" --output "$working/$ledger"

mv "$working/$ledger" "$backup_dir/$ledger"
if [[ "$ledger_only" == false ]]; then mv "$working/$archive" "$backup_dir/$archive"; fi
if [[ -n "$destination" ]]; then
  host=${destination%%:*}
  remote_dir=${destination#*:}
  upload() {
    local filename=$1
    ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15 "$host" \
      "umask 077 && mkdir -p '$remote_dir' && cat > '$remote_dir/.$filename.partial' && mv '$remote_dir/.$filename.partial' '$remote_dir/$filename'" \
      < "$backup_dir/$filename"
  }
  # Publish the deletion sidecar first. A remote dump always has its own sidecar.
  upload "$ledger"
  if [[ "$ledger_only" == false ]]; then upload "$archive"; fi
fi
printf 'Encrypted deletion ledger: %s\n' "$backup_dir/$ledger"
if [[ "$ledger_only" == false ]]; then printf 'Encrypted database backup: %s\n' "$backup_dir/$archive"; fi
