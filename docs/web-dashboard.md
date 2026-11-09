# Web dashboard

Nginx serves Airkeep's browser dashboard and proxies API requests to the Python backend at the same HTTPS address. User view shows readings, seven-day history, personal humidity rules and past alerts. Admin view shows registered Apple devices, Watch widgets, Live Activities, Apple push configuration, recent requests and pending notifications.

Each account has its own humidity rules, past alerts, and device subscriptions. Printer readings and humidity history are shared across the relay. Administrators can also inspect push diagnostics and manage accounts.

## Set up sign-in

For Docker, follow the [account setup instructions](docker-deployment.md#accounts-and-sign-in). When running from source, set the signing key and origin in `server/.env` and run `uv run --directory server manage_accounts.py bootstrap owner`.

The dashboard stays signed in across reloads in the same tab for up to eight hours. Sign in again when opening another tab. Use **My account** to update your password or sign out a connected app. An administrator can reset your password and give you a temporary password that expires after 24 hours.

### Optional single sign-on

Airkeep supports one OpenID Connect provider for the dashboard and Apple apps. Set up a local administrator first, then create a confidential OIDC client with authorization code and S256 PKCE enabled. Register the exact HTTPS redirect URI, including the port if you use one. For example, use `https://airkeep.example.com/auth/oidc/callback`.

In Authentik, create an application with an OAuth2/OIDC provider. Choose the per-application issuer and an asymmetric signing key such as an RSA certificate. Enable **Include claims in id_token** and assign Authentik’s `profile` scope mapping to include usernames and groups. Use an application policy to choose who may sign in. [Authentik OAuth2/OIDC setup](https://docs.goauthentik.io/add-secure-apps/providers/oauth2/) · [Scope mappings](https://docs.goauthentik.io/add-secure-apps/providers/property-mappings/)

Set the application’s **Launch URL** to `https://airkeep.example.com/login/sso` to start single sign-on from the Authentik tile. Authentik can reuse an existing browser session, subject to its consent and MFA policies. Users need an Airkeep account linked to their SSO identity, or automatic account creation must be enabled.

Add the provider settings to the relay’s private `.env` and recreate the relay.

```dotenv
AIRKEEP_OIDC_ISSUER=https://auth.example.com/application/o/airkeep/
AIRKEEP_OIDC_CLIENT_ID=your-client-id
AIRKEEP_OIDC_CLIENT_SECRET=your-client-secret
AIRKEEP_OIDC_LABEL=Authentik
```

Copy the issuer exactly as advertised by the provider, including any trailing slash. Airkeep requests `openid` and `profile` when available. If your provider needs additional scopes, set `AIRKEEP_OIDC_SCOPES` to a space-separated list. The relay must be able to reach the provider’s discovery, token, and signing key endpoints over trusted HTTPS.

In **Admin → Accounts → SSO**, leave automatic creation off to invite users individually, or turn it on to let eligible users create an account on their first sign-in. New accounts start as members. To grant the initial admin role based on provider groups, set the ID token claim name and enter the exact group names. Airkeep checks this mapping at account creation. Manage later role changes in Airkeep.

Create an invitation for a new account or for an existing account you want to link. Share the private link with the intended user. It expires after 24 hours and the first eligible SSO identity to redeem it gets the account. Users redeem the link in a browser and can then sign in on their Apple devices. For existing users, have them redeem the invitation before trying regular SSO sign-in so Airkeep can keep their existing alerts and devices together.

Local sign-in remains available. Disable an account in Airkeep to revoke its sessions immediately. If you remove someone’s provider access, their existing Airkeep sessions can continue until they expire or are revoked in Airkeep.

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

Docker builds `web/dist` into the Nginx image. For live frontend development, run the backend on port 8789 and Vite on port 4173. Vite proxies API calls to the backend:

```sh
uv run --directory server uvicorn app:create_app --factory --host 127.0.0.1 --port 8789
npm run dev --prefix web -- --host 127.0.0.1 --port 4173 --strictPort
```

For this loopback setup only, set `AIRKEEP_WEB_ORIGIN=http://127.0.0.1:4173` and `AIRKEEP_ALLOW_HTTP_LOOPBACK=true` in your ignored development environment. Production browser access uses HTTPS.

## Relay interfaces

Account access JWTs authorize API requests. Alert and event endpoints return only the signed-in user's data. A paired ESP32 can read the compact telemetry snapshot and revoke its own connection. Device and activity registration writes require a native app grant, and Watch widget registrations require a paired Watch grant.

| Endpoint | Purpose |
| --- | --- |
| `GET /auth/session` | Public account setup status |
| `GET/POST /auth/authorize` | Start a PKCE authorization request |
| `POST /auth/credentials` | Verify username and password and complete first-sign-in password change |
| `POST /auth/token` | Exchange a code or rotate a refresh token |
| `POST /auth/device/code` | Start ESP32 pairing |
| `POST /auth/device/verify` and `POST /auth/device/decision` | Review a pairing code and approve or deny it from a signed-in dashboard |
| `GET /auth/me` | Current account identity and role |
| `POST /auth/revoke` | Revoke the current grant and paired Watch grant |
| `GET/DELETE /auth/grants[/{id}]` | List or revoke the user's connections |
| `POST /auth/password` | Change the current password and revoke grants |
| `POST/DELETE /auth/watch` | Pair or disconnect a Watch from an iPhone grant |
| `GET/POST/PATCH /v2/admin/accounts[/{id}]` | Account administration |
| `POST /v2/admin/accounts/{id}/reset` | Issue a temporary password |
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
