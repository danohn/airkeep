# Docker deployment

Run the Airkeep relay on an always-on machine that can reach the printer's LAN. The Dockerfile supports Linux amd64 and arm64; amd64 has been tested. The container runs as UID/GID 10001 with a read-only root filesystem and no Linux capabilities. SQLite data lives in a persistent volume.

## Build and start

From the repository root:

```sh
cd server
mkdir -p private/apns
chmod 700 private private/apns
cp docker.env.example private/relay.env
chmod 600 private/relay.env
```

Edit `private/relay.env` with the printer address, serial, LAN access code and a random bearer token. Generate a token with `openssl rand -hex 32`. Then build and start the relay:

```sh
docker compose build
docker compose up -d
curl --fail http://127.0.0.1:8787/health
```

The base Compose file exposes port 8787 on loopback. `/health` confirms that the API responds. After connecting an app, check that the authenticated snapshot reports a fresh printer reading.

The build uses the repository root as its context and includes the browser frontend. Keep `server/` and `web/` together when copying source to another host. The root `.dockerignore` restricts the context to application source.

The image contains code, built browser assets, locked dependencies and the public Bambu CA bundle. Credentials and databases stay outside it. Keep `private/` out of version control and image archives.

## HTTPS for clients

The Apple apps and ESP32 need a reachable HTTPS URL with a trusted certificate. Point that URL at relay port 8787 through your reverse proxy or private network. DNS and routing must work from each client. Keep the printer's MQTT port private.

The repository includes an optional nginx TLS overlay. To use it:

```sh
cp tls.env.example .env
# Set the hostname, bind address, HTTPS port and certificate directory.
docker compose -f compose.yml -f compose.tls.yml up -d
curl --fail https://airkeep.example.com:8443/health
```

The example uses port 8443; set `AIRKEEP_HTTPS_PORT` to the port you want clients to use. Set `AIRKEEP_BIND_ADDRESS` to an interface reachable by those clients. The certificate directory must contain `fullchain.pem` and `key.pem`, with a certificate valid for `AIRKEEP_HOST`. Set `AIRKEEP_TLS_UID` and `AIRKEEP_TLS_GID` to IDs that can read those files without broadening the private key's permissions.

The overlay mounts certificates read-only. It checks for replacements every 60 seconds, validates them and reloads nginx when valid. Certificate issuance and renewal are managed outside Airkeep.

## Optional APNs

Readings and history work without APNs. For remote alerts and Live Activity updates, follow [push setup](push-setup.md), then place the keys at `private/apns/sandbox.p8` and `private/apns/production.p8`. Set the matching team, key IDs and app topics in `private/relay.env`.

On Linux, restrict the key files to the relay container's user:

```sh
sudo chown -R 10001:10001 private/apns
sudo chmod 700 private/apns
sudo chmod 600 private/apns/sandbox.p8 private/apns/production.p8
docker compose -f compose.yml -f compose.apns.yml up -d
```

Add `-f compose.tls.yml` when using the included TLS overlay. Keys mount read-only at `/run/keys/`. Keep the outer `private/` directory and `relay.env` owned by the operator, with modes 700 and 600.

## Maintain the relay

```sh
docker compose ps
docker compose logs --tail 50
docker compose up -d --build
```

Use the same `-f` options as your deployment when running maintenance commands. Docker restarts the relay after a failure. The health check reports API availability; printer connectivity and reading age are reported separately by the authenticated snapshot.

The data volume preserves history, device registrations, Live Activities and alert settings across container updates. Use SQLite's online backup API, or stop the relay before copying the database and its sidecar files. Back up `private/` separately. `docker compose down` keeps the data volume; adding `--volumes` deletes it.

## Upgrading to shared alerts

Back up SQLite before starting version 0.2.0. The first start migrates the existing rule to Alert 1, preserves its evaluation state and enables subscriptions for existing device registrations. Deploy the relay before updating Apple clients. Older clients can still read telemetry and receive alerts, but their single-rule editor returns an update-required error. Keep the pre-upgrade database and image available for rollback; restoring only the old image will not undo the migration.


## Browser access and Watch widgets

Follow [web dashboard setup](web-dashboard.md) to generate the owner password hash and set `AIRKEEP_WEB_ORIGIN` to the exact HTTPS origin, including its port. Keep the hash single-quoted in `private/relay.env`. Browser assets are built into the existing relay image and served through the same HTTPS route.

Watch widget push updates use their own app topic and APNs key settings. Add topic-authorized Watch keys to the existing read-only private key mount. The iOS and Mac provider credentials stay in place.

Back up SQLite before the upgrade. New migrations add browser sessions, past-alert events, redacted push attempts and Watch widget registrations. The ESP32 and existing Apple bearer APIs keep their current response shapes.
