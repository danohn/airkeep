"""Bambu AMS report parsing shared by the relay and dashboard server."""
import math
import time

from pydantic import BaseModel


class Reading(BaseModel):
    amsID: str
    humidity: float
    temperature: float | None
    rawIndex: int | None
    observedAt: float


def parse_readings(payload: dict, now: float | None = None) -> list[Reading]:
    now = time.time() if now is None else now
    if not isinstance(payload, dict) or not math.isfinite(now):
        return []
    values = []
    report = payload.get("print")
    if not isinstance(report, dict):
        return []
    ams = report.get("ams")
    if not isinstance(ams, dict):
        return []
    units = ams.get("ams") or []
    if not isinstance(units, list):
        return []
    for unit in units[:16]:
        if not isinstance(unit, dict):
            continue
        try:
            identifier = unit.get("id")
            if isinstance(identifier, bool) or not isinstance(identifier, (str, int)):
                continue
            identifier = str(identifier)
            if not 1 <= len(identifier) <= 8 or not identifier.isascii() or not identifier.isdecimal():
                continue
            # Missing humidity_raw is a delta update, never a zero reading.
            if isinstance(unit.get("humidity_raw"), bool):
                continue
            humidity = float(unit["humidity_raw"])
            if not math.isfinite(humidity) or not 1 <= humidity <= 100:
                continue  # Zero is a known boot/sensor fault; do not label it dry.
            temperature = None
            if "temp" in unit:
                try:
                    temp = float(unit["temp"])
                    if math.isfinite(temp) and 0 <= temp <= 100:
                        temperature = temp
                except (ValueError, TypeError, OverflowError):
                    pass
            index = None
            try:
                raw = int(unit.get("humidity", 0))
                if 1 <= raw <= 5:
                    index = raw
            except (ValueError, TypeError, OverflowError):
                pass
            values.append(Reading(amsID=identifier, humidity=humidity,
                                  temperature=temperature, rawIndex=index, observedAt=now))
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    return values
