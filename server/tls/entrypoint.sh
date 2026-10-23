#!/bin/sh
set -eu
: "${AIRKEEP_HOST:?Set the TLS hostname}"
case "$AIRKEEP_HOST" in
    *[!a-zA-Z0-9.-]*|'') echo 'Invalid TLS hostname' >&2; exit 1 ;;
esac
interval="${AIRKEEP_TLS_RELOAD_SECONDS:-60}"
case "$interval" in *[!0-9]*|'') echo 'Invalid reload interval' >&2; exit 1 ;; esac
test "$interval" -ge 5 || { echo 'Reload interval must be at least five seconds' >&2; exit 1; }

envsubst '${AIRKEEP_HOST}' < /etc/nginx/airkeep.conf.template > /tmp/nginx.conf
nginx -t -c /tmp/nginx.conf
fingerprint() { sha256sum /run/tls/fullchain.pem /run/tls/key.pem | sha256sum; }
initial="$(fingerprint)"
nginx -c /tmp/nginx.conf -g 'daemon off;' &
master=$!

watch_certificates() {
    previous="$initial"
    while sleep "$interval"; do
        current="$(fingerprint 2>/dev/null)" || continue
        if [ "$current" != "$previous" ]; then
            if nginx -t -c /tmp/nginx.conf && nginx -s reload -c /tmp/nginx.conf; then
                echo 'Airkeep TLS: renewed certificate loaded' >&2
                previous="$current"
            else
                echo 'Airkeep TLS: replacement certificate rejected; retaining current workers' >&2
            fi
        fi
    done
}
watch_certificates &
watcher=$!
shutdown() {
    kill "$watcher" 2>/dev/null || true
    kill -QUIT "$master" 2>/dev/null || true
    wait "$master" || true
    exit 0
}
trap shutdown TERM INT
set +e
wait "$master"
status=$?
kill "$watcher" 2>/dev/null || true
exit "$status"
