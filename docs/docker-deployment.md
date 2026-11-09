# Docker deployment

Airkeep uses two images. `ghcr.io/danohn/airkeep-relay` reads from the printer and stores its history. `ghcr.io/danohn/airkeep-web` serves the dashboard and forwards API requests to the relay. Both use the `latest` tag by default.

## 1. Set up the printer and URL

From the repository’s `server/` directory on a Docker host that can reach the printer, run:

```sh
./setup.sh
```

The script creates a private `.env` with an API token and signing key, or keeps your existing file. Edit the printer IP, serial number, LAN access code, and HTTPS URL you’ll open on your devices (`AIRKEEP_WEB_ORIGIN`).

<details>
<summary>Set up manually</summary>

1. From `server/`, run `mkdir -p -m 700 private private/apns` to create the directories Compose expects.
2. If you’re starting fresh, run `install -m 600 .env.example .env` to create a private copy of the template. If you already have an `.env`, use that file.
3. Run `openssl rand -hex 32` once for each empty secret. Put one value in `AIRKEEP_API_TOKEN` and the other in `AIRKEEP_JWT_SIGNING_KEY`. Keep any values you already use.
4. Fill in the printer IP, serial number, LAN access code, and `AIRKEEP_WEB_ORIGIN`, then continue to the HTTPS setup below.

</details>

## 2. Connect Airkeep to HTTPS

Airkeep serves HTTP on port `8080` for your reverse proxy. The dashboard and apps require a reachable HTTPS URL with a certificate your devices trust. Set `AIRKEEP_WEB_ORIGIN` to that HTTPS URL.

Use `compose.http.yml` to publish Airkeep’s HTTP endpoint on port `8080` on the Docker host. Configure your preferred reverse proxy to accept HTTPS and forward to `http://127.0.0.1:8080` if it runs on the same host. If it runs on another machine, use the Docker host’s reachable address on port `8080`. After starting Airkeep, you can check the HTTP endpoint from the host with `curl --fail http://127.0.0.1:8080/health`.

<details>
<summary>I have a certificate and want Airkeep to serve HTTPS</summary>

Use `compose.https.yml` and add these settings to `.env`:

```dotenv
AIRKEEP_HOST=airkeep.example.com
AIRKEEP_TLS_DIR=/path/to/certificates
AIRKEEP_BIND_ADDRESS=192.0.2.20
AIRKEEP_HTTPS_PORT=443
```

Use your certificate hostname, the directory containing `fullchain.pem` and `key.pem`, and the Docker host’s IP address on the client network. Point the hostname at that IP. If host port `443` is available, set `AIRKEEP_HTTPS_PORT=443` and use `https://airkeep.example.com` for `AIRKEEP_WEB_ORIGIN`. If you omit the port setting, Airkeep publishes `8443` and the URL becomes `https://airkeep.example.com:8443`.

Nginx checks for renewed certificates in the mounted directory. If the files require a different container user, set `AIRKEEP_TLS_UID` and `AIRKEEP_TLS_GID` (both default to `1000`).

</details>

## Accounts and sign-in

The commands below use `-f compose.http.yml`. If Airkeep serves HTTPS directly, use `-f compose.https.yml`.

If you keep your deployment in a separate directory, you can copy your chosen file there as `compose.yml` and use plain `docker compose` commands. Keep `.env` and `private/` beside it. Remove any `COMPOSE_FILE` setting left over from a previous setup so Compose picks up the default filename.

Pull the images, create your first administrator, then start Airkeep. Replace `owner` with the username you want. The account command prompts for a password of at least 12 characters.

```sh
docker compose -f compose.http.yml pull
docker compose -f compose.http.yml run --rm --no-deps airkeep-relay python manage_accounts.py bootstrap owner
docker compose -f compose.http.yml up -d --wait
```

Open your HTTPS URL with `/health` appended to check the connection. Then open the dashboard and sign in. Check for a recent printer reading. Add users under **Admin → Accounts**.

For an external sign-in provider, see [optional single sign-on](web-dashboard.md#optional-single-sign-on).

<details>
<summary>Recover an administrator account</summary>

Run `docker compose -f compose.http.yml run --rm --no-deps airkeep-relay python manage_accounts.py recover owner` with the administrator’s username and your usual Compose file selection.

</details>

## Optional Apple push notifications

Start with [push setup](push-setup.md) and [Watch widget push setup](web-dashboard.md#watch-widget-updates). For Docker, put the `.p8` keys in `private/apns/` and use container paths such as `/run/keys/sandbox.p8` in `.env`. On Linux, make the directory and keys readable by the relay’s UID and GID `10001`, with mode `700` on the directory and `600` on the keys. After editing `.env`, rerun the same `up -d --wait` command you used to start Airkeep.

## Updates and rollback

Back up SQLite with its online backup API, or stop the relay before copying the database and its sidecar files. Keep a copy of `.env` and `private/` with your backup. Record the running image version before pulling.

```sh
docker compose -f compose.http.yml pull
docker compose -f compose.http.yml up -d --wait --remove-orphans
```

Use the same file selection you used to start Airkeep. To pin or roll back both images, set `AIRKEEP_IMAGE_TAG` to a release tag or `sha-<full commit SHA>`. Restore the matching database backup if the update changed the schema. Use the Compose `down` subcommand to stop the stack and keep the `airkeep-data` database volume.

<details>
<summary>Build the images locally</summary>

Run from the repository root:

```sh
docker build -f server/Dockerfile -t airkeep-relay:local .
docker build -f web/Dockerfile -t airkeep-web:local .
uv run --directory server python ../scripts/check-containers.py
```

To run the local images, set `AIRKEEP_RELAY_IMAGE=airkeep-relay`, `AIRKEEP_WEB_IMAGE=airkeep-web`, and `AIRKEEP_IMAGE_TAG=local` in `.env`, then start Airkeep with your usual Compose file selection and `up -d --pull never`.

See the [container workflow](../.github/workflows/containers.yml) for build and publishing details.

</details>
