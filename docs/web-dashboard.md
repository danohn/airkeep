# Web dashboard

The relay serves Airkeep's browser dashboard at its HTTPS address. User view shows readings, seven-day history, shared humidity rules and past alerts. Admin view shows registered Apple devices, Watch widgets, Live Activities, Apple push configuration, recent requests and pending notifications.

Both views belong to the relay's owner. Admin screens are for inspection. Manage a device's alert subscription in its Apple app.

## Set up browser access

Create the owner password hash from the repository root:

```sh
uv run --directory server setup_web.py
```

The command prompts for a password and confirmation. Copy the generated, single-quoted `AIRKEEP_WEB_PASSWORD_HASH` line into your ignored relay environment file, then set the browser origin:

```dotenv
AIRKEEP_WEB_ORIGIN=https://airkeep.example.com:8443
```

Keep the hash's single quotes when using Docker Compose so the dollar signs remain intact. The browser password is separate from the bearer token used by Apple clients and the ESP32. Browser access becomes available after restarting the relay with both settings configured.

Sign in at the relay's HTTPS address. Sessions last eight hours. Sign out to end a session immediately. Changing the configured password hash invalidates existing sessions. The browser uses an HttpOnly cookie and sends a CSRF header for changes to shared rules.

## History and past alerts

Seven-day charts use five-minute buckets, with the average shown on the line and the minimum and maximum in its tooltip. Shorter ranges use the stored reading resolution. Chart gaps follow interruptions in sensor readings, and the latest source timestamp is retained for each bucket.

Past alerts start when the upgraded relay begins recording transitions. Each rule records one Triggered or Recovered event, including its name, AMS and condition at that time. Events remain available for seven days, including after a rule is renamed or deleted. Filter the timeline by AMS or rule. Past rules keep their saved names in the filter list.

The relay retains redacted push outcomes for seven days, up to 10,000 requests. Admin shows Apple's acceptance response or the request's error and retry state. Device notification permissions remain on each Apple device.

## Watch widget updates

Watch widget registrations have their own tokens and generation counters, separate from humidity-alert delivery. The relay coalesces meaningful display changes and sends at most one widget refresh hint every five minutes per installation. Apple controls when a widget reload runs. The Watch also requests periodic timeline refreshes and keeps the original reading age visible.

Set the Watch app's base topic and topic-authorized APNs key configuration in the private relay environment:

```dotenv
AIRKEEP_WATCH_TOPIC=io.airkeep.watch
AIRKEEP_WATCH_APNS_KEY_ID=YOUR_WATCH_SANDBOX_KEY_ID
AIRKEEP_WATCH_APNS_KEY_PATH=/run/keys/watch-sandbox.p8
AIRKEEP_WATCH_APNS_PRODUCTION_KEY_ID=YOUR_WATCH_PRODUCTION_KEY_ID
AIRKEEP_WATCH_APNS_PRODUCTION_KEY_PATH=/run/keys/watch-production.p8
```

The team ID uses the relay's existing `AIRKEEP_APNS_TEAM_ID`. Existing iOS and Mac key routing stays separate. Operators with a provider key authorized for all required topics can explicitly set `AIRKEEP_WATCH_APNS_USE_SHARED_CREDENTIALS=true` to reuse the ordinary configured keys.

After correcting an Apple authorization error, restart the relay to retry widget updates. Invalid device tokens wait for a new registration.

Mount Watch keys read-only in the same private key directory as other APNs credentials. Keys and browser passwords stay outside the image and Git.

## Local development

Install the browser dependencies and build with Node 22.12 or later. Humidity digits use the locally served Quicksand font under its [SIL Open Font License](../web/public/brand/quicksand-OFL.txt).

From the repository root:

```sh
npm ci --prefix web
npm run build --prefix web
npm test --prefix web
uv run --directory server pytest -q
```

The FastAPI app serves `web/dist` after a build. For live frontend development, run the relay on port 8789 and Vite on port 4173:

```sh
uv run --directory server uvicorn app:create_app --factory --host 127.0.0.1 --port 8789
npm run dev --prefix web -- --host 127.0.0.1 --port 4173 --strictPort
```

For this loopback setup only, set `AIRKEEP_WEB_ORIGIN=http://127.0.0.1:4173` and `AIRKEEP_WEB_SECURE_COOKIES=false` in your ignored development environment. Production browser access uses HTTPS and secure cookies.

## Relay interfaces

Existing bearer APIs remain compatible. Browser sessions can read telemetry, history and events, and manage `/v2/alerts` using the existing revision checks. Device, activity and widget registration writes require the native bearer credential.

| Endpoint | Purpose |
| --- | --- |
| `GET /auth/session` | Browser sign-in state and session CSRF token |
| `POST /auth/login` | Owner password sign-in |
| `POST /auth/logout` | End the current browser session |
| `GET /v2/history?ams=0&hours=168` | Bucketed history, preserving source timestamps and gaps |
| `GET /v2/alert-events` | Newest-first timeline, with optional AMS/rule filter and cursor |
| `GET /v2/admin/overview` | Relay status, storage counts and push configuration |
| `GET /v2/admin/devices` | Redacted Apple device registrations |
| `GET /v2/admin/activities` | Registered Live Activity sessions |
| `GET /v2/admin/widgets` | Redacted Watch widget registrations and pending refreshes |
| `GET /v2/admin/push-attempts` | Redacted, paginated APNs request outcomes |
| `GET /v2/admin/queue` | Pending humidity notifications |
| `PUT/GET/DELETE /v2/widgets/{installationID}` | Native Watch widget registration, status and removal |

Widget PUT requests include a positive monotonic `generation`. Lower generations conflict with HTTP 409. The same generation and identical normalized content is idempotent; different content at that generation conflicts. Removal retains a generation tombstone so old requests cannot restore a removed registration. Pausing widget refreshes uses a newer registration with an empty AMS list.
