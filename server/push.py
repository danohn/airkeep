from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import time
import httpx
import jwt
from config import Settings


@dataclass(frozen=True)
class PushOutcome:
    status: int | None
    reason: str | None = None
    apns_id: str | None = None
    retry_after_seconds: float | None = None


class APNs:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(http2=True, timeout=15)
        self.tokens: dict[str, tuple[float, str]] = {}

    def authorization(self, environment: str, topic: str | None = None):
        now = time.time()
        credentials = self.settings.apns_credentials(environment, topic)
        if credentials is None:
            raise ValueError('APNs credentials missing')
        key_id, key_path = credentials
        cached = self.tokens.get(key_id)
        if cached is None or now - cached[0] > 3000:
            value = jwt.encode({'iss': self.settings.apns_team_id, 'iat': int(now)},
                Path(key_path).read_text(), algorithm='ES256', headers={'kid': key_id})
            self.tokens[key_id] = (now, value)
        return self.tokens[key_id][1]

    async def send_request(self, token: str, base_topic: str, environment: str, payload: dict,
                           *, push_type='alert', expiration=None, collapse_id=None) -> PushOutcome:
        if push_type not in ('alert', 'liveactivity', 'widgets'):
            raise ValueError('Unsupported push type')
        if not self.settings.apns_credentials(environment, base_topic):
            return PushOutcome(None, 'credentials_missing')
        host = 'api.sandbox.push.apple.com' if environment == 'sandbox' else 'api.push.apple.com'
        suffix = '' if push_type == 'alert' else '.push-type.' + push_type
        try:
            headers = {'authorization': f'bearer {self.authorization(environment, base_topic)}',
                       'apns-topic': base_topic + suffix, 'apns-push-type': push_type,
                       'apns-priority': '10' if push_type == 'alert' else '5',
                       'apns-expiration': str(int(expiration if expiration is not None else time.time()+3600))}
            if collapse_id:
                headers['apns-collapse-id'] = collapse_id
            response = await self.client.post(f'https://{host}/3/device/{token}', json=payload, headers=headers)
        except (httpx.HTTPError, OSError, ValueError):
            return PushOutcome(None, 'connection_failed')
        reason = None
        if response.status_code != 200:
            try:
                value = response.json().get('reason')
                if isinstance(value, str):
                    reason = value[:120]
            except (ValueError, AttributeError):
                pass
        retry = None
        value = response.headers.get('retry-after')
        if value:
            try:
                retry = max(0, float(value))
            except ValueError:
                try:
                    retry = max(0, (parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
        return PushOutcome(response.status_code, reason, response.headers.get('apns-id'), retry)

    async def send(self, token: str, topic: str, environment: str, payload: dict, *, live=False):
        result = await self.send_request(token, topic, environment, payload,
                                         push_type='liveactivity' if live else 'alert')
        return result.status or 503

    async def close(self):
        await self.client.aclose()
