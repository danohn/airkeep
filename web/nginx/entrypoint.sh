#!/bin/sh
set -eu
tls="${AIRKEEP_TLS_ENABLED:-false}"
case "$tls" in true|false) ;; *) echo 'AIRKEEP_TLS_ENABLED must be true or false' >&2; exit 1 ;; esac
: > /tmp/airkeep-tls.conf
interval="${AIRKEEP_TLS_RELOAD_SECONDS:-60}"
if [ "$tls" = true ]; then
    : "${AIRKEEP_HOST:?Set the TLS hostname}"
    case "$AIRKEEP_HOST" in *[!a-zA-Z0-9.-]*|'') echo 'Invalid TLS hostname' >&2; exit 1 ;; esac
    case "$interval" in *[!0-9]*|'') echo 'Invalid reload interval' >&2; exit 1 ;; esac
    test "$interval" -ge 5 || { echo 'Reload interval must be at least five seconds' >&2; exit 1; }
    envsubst '${AIRKEEP_HOST}' < /etc/airkeep/tls.conf.template > /tmp/airkeep-tls.conf
fi

nginx -t -c /etc/airkeep/nginx.conf
fingerprint() { sha256sum /run/tls/fullchain.pem /run/tls/key.pem | sha256sum; }
if [ "$tls" = true ]; then initial="$(fingerprint)"; fi
nginx -c /etc/airkeep/nginx.conf -g 'daemon off;' &
master=$!

watch_certificates() {
    previous="$initial"
    while sleep "$interval"; do
        current="$(fingerprint 2>/dev/null)" || continue
        if [ "$current" != "$previous" ]; then
            if nginx -t -c /etc/airkeep/nginx.conf && nginx -s reload -c /etc/airkeep/nginx.conf; then
                echo 'Airkeep web: renewed certificate loaded' >&2
                previous="$current"
            else
                echo 'Airkeep web: replacement certificate rejected; retaining current workers' >&2
            fi
        fi
    done
}
watcher=''
if [ "$tls" = true ]; then watch_certificates & watcher=$!; fi
shutdown() {
    if [ -n "$watcher" ]; then kill "$watcher" 2>/dev/null || true; fi
    kill -QUIT "$master" 2>/dev/null || true
    wait "$master" || true
    exit 0
}
trap shutdown TERM INT
set +e
wait "$master"
status=$?
if [ -n "$watcher" ]; then kill "$watcher" 2>/dev/null || true; fi
exit "$status"
