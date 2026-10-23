from pathlib import Path
import time
import httpx
import jwt
from config import Settings


class APNs:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(http2=True, timeout=15)
        self.tokens: dict[str, tuple[float, str]] = {}

    def authorization(self, environment: str):
        now = time.time()
        credentials = self.settings.apns_credentials(environment)
        if credentials is None:
            raise ValueError("No APNs key configured for this environment")
        key_id, key_path = credentials
        cached = self.tokens.get(key_id)
        if cached is None or now - cached[0] > 3000:
            value = jwt.encode({"iss": self.settings.apns_team_id, "iat": int(now)},
                Path(key_path).read_text(), algorithm="ES256", headers={"kid": key_id})
            self.tokens[key_id] = (now, value)
        return self.tokens[key_id][1]

    async def send(self, token: str, topic: str, environment: str, payload: dict, *, live=False):
        if not self.settings.apns_credentials(environment):
            return 503
        host = "api.sandbox.push.apple.com" if environment == "sandbox" else "api.push.apple.com"
        response = await self.client.post(f"https://{host}/3/device/{token}", json=payload,
            headers={"authorization": f"bearer {self.authorization(environment)}",
                     "apns-topic": topic + (".push-type.liveactivity" if live else ""),
                     "apns-push-type": "liveactivity" if live else "alert",
                     "apns-priority": "5" if live else "10",
                     "apns-expiration": str(int(time.time()) + 3600)})
        return response.status_code

    async def close(self):
        await self.client.aclose()
