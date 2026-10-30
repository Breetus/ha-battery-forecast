"""Synthetic days for planner tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.energy_planner.planner import Slot

BNE = timezone(timedelta(hours=10))


def day_slots(start: datetime, n: int = 80, solar_peak: float = 8.0, load: float = 1.0, prices=None) -> list[Slot]:
    """Half-hour slots with a solar bell curve and a simple Amber-like price shape."""
    slots = []
    for i in range(n):
        t = start + timedelta(minutes=30 * i)
        hr = t.hour + t.minute / 60
        solar = max(0.0, solar_peak * (1 - ((hr - 12) / 6) ** 2)) if 6 < hr < 18 else 0.0
        if prices:
            imp, exp = prices(hr)
        else:
            imp = 0.45 if 16 <= hr < 21 else (0.15 if 0 <= hr < 6 else (0.05 if 10 <= hr < 14 else 0.25))
            exp = 0.30 if 17 <= hr < 20 else 0.03
        slots.append(Slot(t, 0.5, imp, exp, round(solar, 2), load))
    return slots
