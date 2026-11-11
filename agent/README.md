# Airkeep LAN agent

Run one agent on an always-on computer on the same LAN as your Bambu P2S. It subscribes to the printer over verified TLS and sends AMS humidity and temperature to your hosted Airkeep dashboard over outbound HTTPS.

The printer IP, serial and LAN access code stay in the agent's private local SQLite database. The dashboard receives an opaque agent enrollment identity, printer connectivity status, AMS IDs and readings. Setup tests the printer certificate chain, its exact serial and MQTT login before starting dashboard sign-in.

## Run with uv

With [uv installed](https://docs.astral.sh/uv/getting-started/installation/), run:

```sh
uvx airkeep
```

To install the command for regular use:

```sh
uv tool install airkeep
airkeep
```

On first run, open the local setup link printed by Airkeep, normally `http://127.0.0.1:8788/`. The wizard opens on the printer form.

Enter the printer's private LAN IP, serial and eight-character LAN access code. The wizard tests the connection, then begins dashboard sign-in. The default hosted origin is `https://dashboard.airkeep.io`. You can enter your own HTTPS dashboard origin. Follow the displayed verification link, sign in and approve the displayed code. Keep the setup page open until it confirms completion. The setup listener closes automatically, and collection begins.

Airkeep stores its SQLite database in `~/.local/state/airkeep` by default, or in `$XDG_STATE_HOME/airkeep` if set. Running from a checkout with an existing `.local/agent/agent.sqlite3` reuses that directory. To reuse it from another working directory, pass its absolute directory path with `--data-dir`. `AIRKEEP_AGENT_DATA` also sets the data directory. The directory has mode `0700` and newly created files have mode `0600`. Only one process can use each data directory at a time.

For development, run `uv run --locked --project agent airkeep` from the repository root.

## Docker on Linux

The dedicated image is `ghcr.io/danohn/airkeep-agent`, built for `linux/amd64` and `linux/arm64`. Linux host networking lets the agent reach the LAN printer and keeps its setup listener on the host loopback interface.

```sh
docker run -d --name airkeep-agent \
  --network host --restart unless-stopped \
  --mount type=volume,source=airkeep-agent,target=/data \
  ghcr.io/danohn/airkeep-agent:latest
```

Open the local setup page to configure the printer.

For a headless Linux host, forward its loopback port over SSH and open `http://127.0.0.1:8788` on your computer.

```sh
ssh -L 8788:127.0.0.1:8788 user@agent-host
```

The host needs outbound TCP 8883 to the printer and outbound TCP 443 to the dashboard. IPv6 ULA and link-local printer addresses are supported alongside IPv4 private and link-local addresses. The setup port defaults to 8788 and can be changed with `--setup-port`. The wizard validates the exact loopback Host and Origin, so use the same local port when forwarding it.

Build a local image from the repository root with the following command.

```sh
docker build -f agent/Dockerfile -t airkeep-agent:local .
```

The `LAN agent image` workflow tests and builds pull requests, then publishes the dedicated multi-platform image on main pushes and version tags. For a pinned release, replace `latest` with the published version or image digest.

## Buffering and reconnecting

The agent persists at most one reading per AMS every 30 seconds, retaining unsent readings for 24 hours. It uploads up to 100 readings per request. Fresh observations from the last 30 seconds are sent first, newest first, followed by replay history in original observation order. Each persisted event has a stable UUID. Successful HTTP 204 responses remove acknowledged events. Failed requests keep them for retry with exponential backoff and jitter. HTTP 429 responses honor a numeric Retry-After delay up to one hour.

Readings retained across restarts, failed deliveries or beyond the 30-second live window carry `replay=true` and their original timestamps. A new observation remains eligible for live delivery while older history is buffered. MQTT reconnects automatically after interrupted LAN connections. Cloud status updates include printer connectivity on transitions and at least every 30 seconds while the dashboard is reachable. A revoked enrollment stops uploads and leaves a generic `enrollment_required` status in the process output. The process continues collecting within the same 24-hour retention window.

If the dashboard permanently rejects an ingest request with HTTP 400 or 422, the agent reduces the batch size until it isolates the rejected reading. It then moves only that reading into a private local quarantine and continues delivering valid readings with their original event IDs. The quarantine retains at most 100 readings for 24 hours. Process output reports `invalid_reading_quarantined` and then `connected_with_quarantined_readings`, without including event contents or upstream error details. Check the host clock and agent version when this status appears. Successful replacement enrollment clears the quarantine along with previous buffered readings.

To replace credentials or enroll again, stop the running agent and run the same command with `--setup`. In Docker, stop the service and run an interactive replacement using the same volume.

```sh
docker stop airkeep-agent
docker run --rm -it --network host \
  --mount type=volume,source=airkeep-agent,target=/data \
  ghcr.io/danohn/airkeep-agent:latest airkeep --setup
```

This process continues running after setup. Stop it with Ctrl-C, then restart the original container with `docker start airkeep-agent`. Successful replacement enrollment atomically clears previous buffered readings and creates a new installation identity, preventing delivery of a previous account's history into the replacement account. An interrupted replacement keeps the saved enrollment intact.

## Checks

```sh
uv run --locked --directory agent pytest -q
```

The tests cover queue persistence, 24-hour pruning, sample throttling, event ordering and IDs, retries, replay, heartbeat transitions, revoked enrollment, device-flow pacing, local setup and TLS serial validation. Testing against a physical P2S requires that printer's LAN settings and network access.

## Publish to PyPI

Configure the [PyPI Trusted Publisher](https://pypi.org/manage/account/publishing/) for project `airkeep`, owner `danohn`, repository `airkeep`, workflow `publish-agent.yml`, and environment `pypi`. Use a pending publisher before the first release. Configure the GitHub `pypi` environment to accept release tags matching `agent-v*`.

Update the version in `agent/pyproject.toml`, then regenerate the agent and server lockfiles. Push a matching tag such as `agent-v0.1.0` to publish the wheel and source distribution. The workflow tests the agent and checks the tag against the package version before publishing. [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
