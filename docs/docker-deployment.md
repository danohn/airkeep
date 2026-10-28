# Docker deployment

Airkeep runs as two containers. `airkeep` collects MQTT readings, stores history and rules, and serves the Python API. `web` serves the built dashboard through Nginx and proxies API requests to `airkeep:8787`. The browser and native clients use the same address.

| Image | Contents |
| --- | --- |
| `ghcr.io/danohn/airkeep-relay` | Python backend and locked runtime dependencies |
| `ghcr.io/danohn/airkeep-web` | Nginx, dashboard assets and optional HTTPS support |

Both images run without root, with read-only filesystems. Only the backend mounts the data volume and APNs keys. Printer credentials and browser password configuration stay in the backend's private environment file.

## Pull and start

Use a host with Docker Compose that can reach the printer's LAN. From the repository root:

```sh
cd server
mkdir -p private/apns
chmod 700 private private/apns
cp docker.env.example private/relay.env
chmod 600 private/relay.env
```

Set the printer address, serial, LAN access code and a random bearer token in `private/relay.env`. Generate a token with `openssl rand -hex 32`.

Choose a published tag in an ignored `.env` next to `compose.yml`:

```dotenv
AIRKEEP_IMAGE_TAG=main
```

Both services use that tag. `main` follows successful main-branch builds. For a deployment you want to keep fixed, use `sha-` followed by the full source commit SHA from a successful publishing run. Version tags such as `0.3.1` become available when the corresponding `v0.3.1` Git tag is published.

After the first publishing workflow succeeds:

```sh
docker compose pull
docker compose up -d
curl --fail http://127.0.0.1:8787/health
```

Port 8787 on loopback reaches Nginx. The Python backend is reachable within the Compose network. `/health` checks the backend through the proxy; `/web-health` checks Nginx. Check printer connectivity and reading age separately in an app or the Admin dashboard.

GitHub creates new container packages with private visibility. The package owner can make both packages public in GitHub's package settings for anonymous pulls. Private packages require `docker login ghcr.io` with a credential that can read them. See [GitHub's Container registry guide](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

For a fork or another registry, set `AIRKEEP_RELAY_IMAGE` and `AIRKEEP_WEB_IMAGE` to the image names, keeping the shared tag in `AIRKEEP_IMAGE_TAG`.

## HTTPS for clients

Give every client a reachable HTTPS URL with a trusted certificate. An existing reverse proxy can forward that address to Nginx on loopback port 8787. Keep the printer's MQTT port private.

To let the Airkeep web container handle HTTPS with an existing certificate:

```sh
cp tls.env.example .env
# Set the image tag, hostname, bind address, HTTPS port and certificate directory.
docker compose -f compose.yml -f compose.tls.yml pull
docker compose -f compose.yml -f compose.tls.yml up -d
curl --fail https://airkeep.example.com:8443/health
```

The overlay configures the same web container to listen on HTTPS port 8443. Its loopback HTTP port remains available for local checks and reverse proxies. Set `AIRKEEP_HTTPS_PORT` and `AIRKEEP_BIND_ADDRESS` for the host interface and port clients will reach.

The certificate directory must contain `fullchain.pem` and `key.pem`, with a certificate valid for `AIRKEEP_HOST`. Set `AIRKEEP_TLS_UID` and `AIRKEEP_TLS_GID` to IDs that can read the files while keeping the private key's permissions restricted.

The web container mounts that directory read-only. It checks for certificate replacements every 60 seconds, validates them and reloads Nginx when valid. Manage certificate issuance and renewal outside Airkeep.

## Browser access

Generate the owner password hash using the backend image:

```sh
docker compose run --rm --no-deps airkeep python setup_web.py
```

Copy the generated, single-quoted `AIRKEEP_WEB_PASSWORD_HASH` line into `private/relay.env`, retaining its quotes. Set `AIRKEEP_WEB_ORIGIN` to the exact browser HTTPS origin, including its port. Restart the backend with your usual Compose options, then sign in at that address. [Web dashboard setup](web-dashboard.md) covers sessions, history and Admin views.

Nginx serves the static dashboard. Python handles browser login, session cookies, CSRF checks and data access. The bearer token used by native clients stays out of the frontend image.

## Optional APNs

Follow [push setup](push-setup.md), then place keys in `private/apns/` and set the team, key IDs and app topics in `private/relay.env`.

On Linux, restrict the key directory to the backend container's user:

```sh
sudo chown -R 10001:10001 private/apns
sudo chmod 700 private/apns
sudo chmod 600 private/apns/*.p8
docker compose -f compose.yml -f compose.apns.yml up -d
```

Add `-f compose.tls.yml` when using the included HTTPS support. Keys mount read-only at `/run/keys/` in the backend. Keep the outer `private/` directory and `relay.env` owned by the operator, with modes 700 and 600.

Watch widget updates use their own app topic and key settings. [Watch push setup](web-dashboard.md#watch-widget-updates) covers separate topic-authorized keys and explicitly sharing an authorized provider key.

## Updates and rollback

Pull both images before replacing containers:

```sh
docker compose pull
docker compose up -d --remove-orphans
docker compose ps
docker compose logs --tail 50
```

Use the same `-f` options as your deployment for every command. Set the previous shared image tag to roll back both services together. Keep the previous images available until the update is verified.

The `airkeep-data` volume preserves readings, rules, subscriptions, sessions and registrations. Back up SQLite before upgrades using its online backup API, or stop the backend before copying the database and its sidecar files. Back up `private/` and the Compose configuration separately. `docker compose down` keeps the data volume; `--volumes` removes it.

### Move from the combined image

Keep the existing Compose project name and data volume. While the old Compose files are still installed, stop the old `tls` service if you use the TLS overlay:

```sh
docker compose -f compose.yml -f compose.tls.yml stop tls
```

Install the new Compose files, select a matching pair of published image tags, then run `pull` and `up -d --remove-orphans` with your overlays. The new `web` service takes over HTTPS and the loopback HTTP port. The `airkeep` service keeps its database volume and private configuration. Existing app URLs and bearer credentials can stay the same.

Older single-rule databases migrate to shared alerts on backend startup. Keep a pre-upgrade database backup for rollback across that schema migration.

## Build locally

The optional build overlay uses source from the repository root:

```sh
docker compose -f compose.yml -f compose.build.yml build
docker compose -f compose.yml -f compose.build.yml up -d
```

Use the build overlay for subsequent commands in that deployment, alongside any APNs or TLS overlays. Local image names are `airkeep-relay:local` and `airkeep-web:local` by default. The repository's `.dockerignore` excludes runtime configuration, keys, databases and local build artifacts.

For a container integration check, from the repository root:

```sh
uv run --directory server python ../scripts/check-containers.py
```

The check uses the local images in an isolated network with disposable credentials and a test certificate. It exercises static files, HTTPS, native bearer requests, browser login, CSRF and rule conflicts, and confirms that Nginx serves the frontend while the backend is stopped. Its test containers and temporary data are removed afterward.

## Image publishing

[Container images](../.github/workflows/containers.yml) runs relay and browser tests, builds both images and checks the two-container stack on pull requests. Publishing runs after successful checks on main, `v*` tags or a manual run on main. Pull requests run checks without publishing.

The publishing jobs build `linux/amd64` and `linux/arm64` images. Each image gets the full `sha-<commit>` tag. Main builds also get `main`; stable version tags get their version, major/minor and `latest` tags. Use the same tag for both images.

Publishing uses the workflow's `GITHUB_TOKEN` with `packages: write`. Runtime credentials and Apple keys belong on the Docker host. They are excluded from both image builds.
