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
