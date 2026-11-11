"""HTTPS-only device enrollment and idempotent telemetry delivery."""
import random
import time
from urllib.parse import urlsplit

import httpx

CLIENT_ID = 'airkeep-relay'
DEFAULT_ORIGIN = 'https://dashboard.airkeep.io'


def https_origin(value):
    url = urlsplit(value)
    if (url.scheme != 'https' or not url.hostname or url.username is not None or url.password is not None
            or url.path not in ('', '/') or '?' in value or '#' in value
            or any(character.isspace() for character in value)):
        raise ValueError('Enter an HTTPS dashboard origin without a path or credentials.')
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError('Enter a valid HTTPS port.')
    return value.rstrip('/')


class Cloud:
    def __init__(self, origin, client=None):
        self.origin = https_origin(origin)
        self.client = client or httpx.Client(timeout=10, follow_redirects=False, trust_env=False)

    def post(self, path, **kwargs):
        # Explicitly prohibit redirects even for an injected/test client.
        return self.client.post(self.origin + path, follow_redirects=False, **kwargs)

    def begin(self, installation_id):
        response = self.post('/auth/device/code', data={'client_id': CLIENT_ID, 'installation_id': installation_id})
        if response.status_code != 200:
            raise ValueError('The dashboard could not begin enrollment. Try again.')
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError('The dashboard returned an invalid enrollment response.')
        if not isinstance(data.get('verification_uri'), str):
            raise ValueError('The dashboard returned an invalid verification address.')
        uri = urlsplit(data['verification_uri'])
        if uri.scheme != 'https' or not uri.hostname or uri.username is not None or uri.password is not None:
            raise ValueError('The dashboard returned an invalid verification address.')
        expires, interval = data.get('expires_in'), data.get('interval', 5)
        if (not isinstance(expires, (int, float)) or not isinstance(interval, (int, float))
                or not 1 <= expires <= 86400 or not 1 <= interval <= 300):
            raise ValueError('The dashboard returned invalid enrollment timing.')
        if not all(isinstance(data.get(key), str) and data[key] for key in ('device_code', 'user_code')):
            raise ValueError('The dashboard returned invalid enrollment codes.')
        return data

    def poll(self, device_code):
        response = self.post('/auth/token', data={'client_id': CLIENT_ID,
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code', 'device_code': device_code})
        if response.status_code == 200:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError('The dashboard returned an invalid enrollment response.')
            token = data.get('access_token')
            kind = data.get('token_type')
            if not isinstance(kind, str) or kind.lower() != 'bearer' or not isinstance(token, str) or not token:
                raise ValueError('The dashboard returned an invalid enrollment token.')
            return {'access_token': token}
        if response.status_code == 400:
            data = response.json()
            error = data.get('error') if isinstance(data, dict) else None
            if error in ('authorization_pending', 'slow_down', 'access_denied', 'expired_token'):
                return {'error': error}
        raise ValueError('The dashboard could not check enrollment. Try again.')

    def close(self):
        self.client.close()


class Uploader:
    def __init__(self, cloud, spool, token):
        self.cloud, self.spool, self.token = cloud, spool, token
        self.next_attempt = 0
        self.heartbeat_at = 0
        self.last_connected = None
        self.failures = 0
        self.revoked = False
        self.status = 'connecting'

    def step(self, connected, now=None):
        now = time.monotonic() if now is None else now
        if self.revoked or now < self.next_attempt:
            return
        batch = self.spool.batch()
        heartbeat = self.last_connected != connected or now >= self.heartbeat_at
        if not batch and not heartbeat:
            return
        body = {'readings': batch}
        if heartbeat:
            body['printerConnected'] = connected
        try:
            response = self.cloud.post('/v1/ingest', headers={'Authorization': 'Bearer ' + self.token}, json=body)
            status = response.status_code
        except httpx.HTTPError:
            status = 0
        if status == 204:
            self.spool.acknowledge(batch)
            self.failures = 0
            self.next_attempt = now
            self.status = 'connected'
            if heartbeat:
                self.last_connected = connected
                self.heartbeat_at = now + 30
            return
        self.spool.mark_replay()
        if status in (401, 403):
            self.revoked = True
            self.status = 'enrollment_required'
            return
        self.status = 'retrying'
        self.failures = min(self.failures + 1, 8)
        delay = min(300, 2 ** self.failures) * random.uniform(0.8, 1.2)
        if status == 429:
            try:
                delay = max(delay, min(3600, float(response.headers.get('Retry-After', 0))))
            except ValueError:
                pass
        self.next_attempt = now + delay
