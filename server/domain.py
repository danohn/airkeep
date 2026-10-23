"""Telemetry parsing and alert rules used by HTTP, MQTT and APNs."""
from dataclasses import dataclass
import math
import time
from pydantic import BaseModel, Field, model_validator


class Reading(BaseModel):
    amsID: str
    humidity: float
    temperature: float | None
    rawIndex: int | None
    observedAt: float


def parse_readings(payload: dict, now: float | None = None) -> list[Reading]:
    now = time.time() if now is None else now
    values = []
    units = ((payload.get("print") or {}).get("ams") or {}).get("ams") or []
    if not isinstance(units, list):
        return []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        try:
            # Missing humidity_raw is a delta update, never a zero reading.
            humidity = float(unit["humidity_raw"])
            if not math.isfinite(humidity) or not 1 <= humidity <= 100:
                continue  # Zero is a known boot/sensor fault; do not label it dry.
            temperature = None
            if "temp" in unit:
                try:
                    temp = float(unit["temp"])
                    if math.isfinite(temp) and 0 <= temp <= 100:
                        temperature = temp
                except (ValueError, TypeError):
                    pass
            index = None
            try:
                raw = int(unit.get("humidity", 0))
                if 1 <= raw <= 5:
                    index = raw
            except (ValueError, TypeError):
                pass
            values.append(Reading(amsID=str(unit["id"]), humidity=humidity,
                                  temperature=temperature, rawIndex=index, observedAt=now))
        except (KeyError, ValueError, TypeError):
            continue
    return values


class AlertRule(BaseModel):
    enabled: bool = False
    amsID: str = Field(default="0", min_length=1, max_length=8)
    threshold: float = Field(default=30, ge=5, le=95)
    holdSeconds: int = Field(default=300, ge=0, le=86400)
    hysteresis: float = Field(default=3, ge=1, le=20)

    @model_validator(mode="after")
    def valid_reset(self):
        if self.hysteresis >= self.threshold:
            raise ValueError("Hysteresis must be smaller than the threshold")
        return self


@dataclass
class AlertState:
    since: float | None = None
    active: bool = False

    def evaluate(self, rule: AlertRule, reading: Reading | None, now: float, stale: int) -> str | None:
        if not rule.enabled:
            self.since, self.active = None, False
            return None
        if reading is None or now - reading.observedAt > stale:
            # An interrupted stream cannot satisfy a continuous hold.
            self.since = None
            return None
        if self.active:
            if reading.humidity <= rule.threshold - rule.hysteresis:
                self.active, self.since = False, None
                return "recovered"
            return None
        if reading.humidity >= rule.threshold:
            if self.since is None:
                self.since = now
            if now - self.since >= rule.holdSeconds:
                self.active = True
                return "high"
        else:
            self.since = None
        return None


def activity_payload(reading: Reading | None, now: float, stale: int, *, end: bool = False) -> dict:
    sampled = reading.observedAt if reading else now - stale - 1
    # Use Unix seconds because Swift Date's default encoding uses another epoch.
    state = {"humidity": reading.humidity if reading else None,
             "temperature": reading.temperature if reading else None,
             "observedAt": sampled, "connected": now - sampled <= stale}
    aps = {"timestamp": int(now), "event": "end" if end else "update",
           "content-state": state, "stale-date": int(sampled + stale),
           # Match the app's relevance score. Omitting it resets the value to zero,
           # which left the session missing from Dynamic Island during testing.
           "relevance-score": 50}
    if end:
        aps["dismissal-date"] = int(now)
    return {"aps": aps}
