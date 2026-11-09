from pathlib import Path
from typing import Literal
from pydantic import Field, SecretStr, model_validator
from urllib.parse import urlsplit
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIRKEEP_", env_file=".env", extra="ignore", hide_input_in_errors=True)
    printer_ip: str = ""
    printer_serial: str = ""
    printer_code: str = ""
    db_path: Path = Path(".local/airkeep.sqlite3")
    ca_path: Path = Path(__file__).parent / "bambu-ca.pem"
    mqtt_enabled: bool = True
    stale_seconds: int = 90
    apns_team_id: str = ""
    apns_key_id: str = ""
    apns_key_path: str = ""
    apns_key_environment: Literal["sandbox", "production", "both"] = "both"
    apns_production_key_id: str = ""
    apns_production_key_path: str = ""
    ios_topic: str = "io.airkeep"
    mac_topic: str = "io.airkeep.mac"
    watch_topic: str = "io.airkeep.watch"
    watch_apns_key_id: str = ""
    watch_apns_key_path: str = ""
    watch_apns_production_key_id: str = ""
    watch_apns_production_key_path: str = ""
    watch_apns_use_shared_credentials: bool = False
    jwt_signing_key: str = ""
    web_password_hash: str = ""
    web_origin: str = ""
    allow_http_loopback: bool = False
    web_secure_cookies: bool = True
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = SecretStr("")
    oidc_label: str = Field(default="Single sign-on", min_length=1, max_length=60)
    oidc_scopes: str = ""

    @model_validator(mode='after')
    def browser_settings(self):
        if self.web_origin:
            value = urlsplit(self.web_origin)
            if value.username or value.password or value.query or value.fragment or value.path not in ('','/') or not value.hostname:
                raise ValueError('AIRKEEP_WEB_ORIGIN must be a plain browser origin')
            loopback = value.hostname in ('localhost','127.0.0.1','::1')
            local_http = loopback and (self.allow_http_loopback or not self.web_secure_cookies)
            if value.scheme != 'https' and not (value.scheme=='http' and local_http):
                raise ValueError('Browser access needs HTTPS; plain HTTP is available only for explicit loopback development')
            if not self.web_secure_cookies and not loopback:
                raise ValueError('Insecure browser cookies are restricted to loopback development')
        if self.jwt_signing_key and (len(self.jwt_signing_key.encode()) < 32 or not self.web_origin):
            raise ValueError('Set a signing key of at least 32 bytes and the dashboard origin.')
        if self.web_password_hash and not self.web_password_hash.startswith('$argon2id$'):
            raise ValueError('AIRKEEP_WEB_PASSWORD_HASH must be an Argon2id hash from setup_web.py')
        credentials = (self.oidc_issuer, self.oidc_client_id, self.oidc_client_secret.get_secret_value())
        if any(credentials):
            issuer = urlsplit(self.oidc_issuer)
            if (not all(credentials) or not self.jwt_signing_key or not self.web_origin.startswith('https://')
                    or issuer.scheme != 'https' or not issuer.hostname or issuer.username is not None
                    or issuer.query or issuer.fragment or '?' in self.oidc_issuer or '#' in self.oidc_issuer):
                raise ValueError('OIDC needs an HTTPS issuer and Airkeep origin, a client ID, a client secret, and a signing key.')
        return self

    @property
    def apns_ready(self) -> bool:
        return bool(self.apns_credentials("sandbox") or self.apns_credentials("production"))

    def apns_credentials(self, environment: str, topic: str | None = None) -> tuple[str, str] | None:
        if not self.apns_team_id:
            return None
        if topic == self.watch_topic:
            pair = (self.watch_apns_production_key_id, self.watch_apns_production_key_path) if environment == "production" else (self.watch_apns_key_id, self.watch_apns_key_path)
            if all(pair):
                return pair
            if not self.watch_apns_use_shared_credentials:
                return None
        if environment == "production" and self.apns_production_key_id and self.apns_production_key_path:
            return self.apns_production_key_id, self.apns_production_key_path
        if self.apns_key_environment in (environment, "both") and self.apns_key_id and self.apns_key_path:
            return self.apns_key_id, self.apns_key_path
        return None

    def notification_configuration(self, platform: str, environment: str, bundle_id: str) -> tuple[str, str]:
        """Check local provider setup without contacting APNs or exposing key paths."""
        from cryptography.exceptions import UnsupportedAlgorithm
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        credentials = self.apns_credentials(environment, self.watch_topic if platform == "watch" else None)
        if credentials is None:
            return "missing", "credentials_missing"
        topic = self.watch_topic if platform == "watch" else self.mac_topic if platform == "mac" else self.ios_topic
        if not topic or topic != bundle_id:
            return "invalid", "topic_mismatch"
        try:
            data = Path(credentials[1]).read_bytes()
        except OSError:
            return "invalid", "key_unreadable"
        try:
            key = serialization.load_pem_private_key(data, password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            return "invalid", "key_invalid"
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            return "invalid", "key_invalid"
        # A valid local key cannot establish Apple's topic authorization or delivery.
        return "available", "local_checks_passed"
