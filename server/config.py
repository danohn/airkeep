from pathlib import Path
from typing import Literal
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIRKEEP_", env_file=".env", extra="ignore")
    printer_ip: str = ""
    printer_serial: str = ""
    printer_code: str = ""
    api_token: str = Field(min_length=32)
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

    @property
    def apns_ready(self) -> bool:
        return bool(self.apns_credentials("sandbox") or self.apns_credentials("production"))

    def apns_credentials(self, environment: str) -> tuple[str, str] | None:
        if not self.apns_team_id:
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

        credentials = self.apns_credentials(environment)
        if credentials is None:
            return "missing", "credentials_missing"
        topic = self.mac_topic if platform == "mac" else self.ios_topic
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
