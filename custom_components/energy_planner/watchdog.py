"""Spots when the plan isn't being carried out, for phone alerts.

No Home Assistant imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# How long a problem must last (about three 5-minute retries) before an alert.
ALERT_AFTER = timedelta(minutes=15)
# A forced charge or discharge must move the SoC by at least this share of
# what its power should have done, measured over this long.
MIN_RESPONSE = 0.25
RESPONSE_WINDOW = timedelta(minutes=10)


@dataclass
class Incident:
    key: str
    message: str
    since: datetime
    alerted: bool = False


class Watchdog:
    """Turns per-update problem lists into one alert per incident."""

    def __init__(self, delay: timedelta = ALERT_AFTER) -> None:
        self.delay = delay
        self.incidents: dict[str, Incident] = {}

    def update(
        self, now: datetime, problems: dict[str, str], delays: dict[str, timedelta] | None = None
    ) -> tuple[list[str], list[str]]:
        """Return (new alerts, resolved alerts) for this update."""
        resolved = []
        for key in list(self.incidents):
            if key not in problems:
                inc = self.incidents.pop(key)
                if inc.alerted:
                    resolved.append(inc.message)
        alerts = []
        for key, message in problems.items():
            inc = self.incidents.get(key)
            if inc is None:
                inc = self.incidents[key] = Incident(key, message, now)
            else:
                inc.message = message
            delay = (delays or {}).get(key, self.delay)
            if not inc.alerted and now - inc.since >= delay:
                inc.alerted = True
                alerts.append(message)
        return alerts, resolved

    def blocked(self) -> list[str]:
        """Messages for incidents that have lasted long enough to alert."""
        return [i.message for i in self.incidents.values() if i.alerted]

    def clear(self) -> None:
        self.incidents.clear()


@dataclass
class ResponseTracker:
    """Remembers the SoC when a forced mode started, to check the battery follows."""

    mode: str | None = None
    power_kw: float = 0.0
    since: datetime | None = None
    soc: float | None = None

    def problem(
        self, mode: str, power_kw: float, now: datetime, soc: float | None, capacity_kwh: float
    ) -> str | None:
        if mode != self.mode or abs(power_kw - self.power_kw) >= 1.0 or self.since is None:
            self.mode, self.power_kw, self.since, self.soc = mode, power_kw, now, soc
            return None
        if soc is None or self.soc is None or capacity_kwh <= 0:
            return None
        minutes = (now - self.since).total_seconds() / 60
        if minutes < RESPONSE_WINDOW.total_seconds() / 60:
            return None
        moved = soc - self.soc
        if mode == "hold":
            # Solar may charge it, but it shouldn't fall.
            if moved < -2.0:
                return f"Battery is meant to be held but has dropped {abs(moved):.0f}% in {minutes:.0f} min"
            return None
        expected = power_kw * minutes / 60 / capacity_kwh * 100
        if mode == "charge" and soc < 98 and moved < expected * MIN_RESPONSE:
            return f"Battery should be charging at {power_kw:g} kW but has only gone from {self.soc:.0f}% to {soc:.0f}% in {minutes:.0f} min"
        if mode == "discharge" and soc > 2 and -moved < expected * MIN_RESPONSE:
            return f"Battery should be selling at {power_kw:g} kW but has only gone from {self.soc:.0f}% to {soc:.0f}% in {minutes:.0f} min"
        return None

    def reset(self) -> None:
        self.mode, self.since, self.soc = None, None, None
