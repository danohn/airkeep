#!/bin/sh
set -euC

CDPATH= cd "$(dirname "$0")"
umask 077
mkdir -p private/apns

if [ -e .env ] || [ -L .env ]; then
    printf '%s\n' 'Keeping the existing .env.'
    exit 0
fi

api_token=$(openssl rand -hex 32)
signing_key=$(openssl rand -hex 32)
sed -e "s/^AIRKEEP_API_TOKEN=$/AIRKEEP_API_TOKEN=$api_token/" \
    -e "s/^AIRKEEP_JWT_SIGNING_KEY=$/AIRKEEP_JWT_SIGNING_KEY=$signing_key/" \
    .env.example > .env

printf '%s\n' 'Created .env. Edit the printer settings and HTTPS URL before starting Airkeep.'
