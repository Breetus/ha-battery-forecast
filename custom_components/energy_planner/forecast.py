"""Turn raw price, solar and load data into planner slots.

No Home Assistant imports: everything here takes plain Python data.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .planner import Slot


@dataclass
class Interval:
    start: datetime
    end: datetime
    value: float


def parse_time(value) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_DURATION = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?$")


def _parse_duration(value) -> timedelta | None:
    if isinstance(value, (int, float)):
        return timedelta(minutes=value)
    if isinstance(value, str):
        m = _DURATION.match(value)
        if m and (m.group(1) or m.group(2)):
            return timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))
    return None


def parse_amber(intervals: list[dict]) -> tuple[list[Interval], list[Interval]]:
    """Split Amber intervals into (import $/kWh, export $/kWh earned).

    Accepts both the raw Amber API shape that PowerSync keeps (camelCase,
    c/kWh, feed-in negative when you earn) and the official Home Assistant
    Amber integration's forecast attributes (snake_case, $/kWh, feed-in
    already positive when you earn).
    """
    imports: dict[datetime, Interval] = {}
    exports: dict[datetime, Interval] = {}
    for item in intervals or []:
        if not isinstance(item, dict):
            continue
        if "perKwh" in item:  # raw Amber API
            start = parse_time(item.get("startTime"))
            end = parse_time(item.get("endTime"))
            price = item.get("perKwh")
            advanced = item.get("advancedPrice")
            if item.get("type") == "ForecastInterval" and isinstance(advanced, dict):
                price = advanced.get("predicted", price)
            if price is None:
                continue
            price = float(price) / 100
            channel = item.get("channelType")
            if channel == "feedIn":
                price = -price
        elif "per_kwh" in item:  # HA Amber integration attribute
            start = parse_time(item.get("start_time"))
            end = parse_time(item.get("end_time"))
            price = item.get("advanced_price_predicted")
            if price is None:
                price = item.get("per_kwh")
            if price is None:
                continue
            price = float(price)
            channel = item.get("_channel", "general")
        else:
            continue
        if start is None or end is None or end <= start:
            continue
        # Amber starts are one second after the boundary (02:00:01-02:30:00).
        start = start.replace(second=0, microsecond=0)
        target = exports if channel == "feedIn" else imports
        # Prefer the shortest interval for a start time (5-minute actuals beat
        # the 30-minute forecast they sit inside).
        existing = target.get(start)
        if existing is None or (end - start) < (existing.end - existing.start):
            target[start] = Interval(start, end, price)
    return sorted(imports.values(), key=lambda i: i.start), sorted(exports.values(), key=lambda i: i.start)


def parse_solar(periods: list[dict], key: str = "pv_estimate") -> list[Interval]:
    """Parse Solcast periods (API or HA integration shape) into kW intervals."""
    out = []
    for item in periods or []:
        if not isinstance(item, dict):
            continue
        kw = item.get(key, item.get("pv_estimate"))
        if kw is None:
            continue
        length = _parse_duration(item.get("period")) or timedelta(minutes=30)
        start = parse_time(item.get("period_start"))
        end = parse_time(item.get("period_end"))
        if start is None and end is not None:
            start = end - length
        if end is None and start is not None:
            end = start + length
        if start is None or end <= start:
            continue
        out.append(Interval(start, end, float(kw)))
    out.sort(key=lambda i: i.start)
    return out


def average_over(intervals: list[Interval], start: datetime, end: datetime) -> float | None:
    """Time-weighted mean of the intervals overlapping [start, end)."""
    total = covered = 0.0
    for iv in intervals:
        if iv.end <= start:
            continue
        if iv.start >= end:
            break
        overlap = (min(iv.end, end) - max(iv.start, start)).total_seconds()
        if overlap > 0:
            total += iv.value * overlap
            covered += overlap
    if covered <= 0:
        return None
    return total / covered


def _dedupe(intervals: list[Interval]) -> list[Interval]:
    """Remove overlaps, letting shorter intervals (5-minute actuals) win."""
    taken: list[Interval] = []
    for iv in sorted(intervals, key=lambda i: i.end - i.start):
        pieces = [(iv.start, iv.end)]
        for t in taken:
            nxt = []
            for a, b in pieces:
                if t.end <= a or t.start >= b:
                    nxt.append((a, b))
                    continue
                if a < t.start:
                    nxt.append((a, t.start))
                if t.end < b:
                    nxt.append((t.end, b))
            pieces = nxt
        taken += [Interval(a, b, iv.value) for a, b in pieces if b > a]
    return sorted(taken, key=lambda i: i.start)


def load_profile_kw(profile: list[float], when: datetime) -> float:
    """Look up a 48-bin (half-hourly) load profile at a local time."""
    if not profile:
        return 0.0
    bins = len(profile)
    idx = int((when.hour * 60 + when.minute) / (1440 / bins)) % bins
    return profile[idx]


def build_slots(
    now: datetime,
    imports: list[Interval],
    exports: list[Interval],
    solar: list[Interval],
    load_profile: list[float],
    tz,
    slot_minutes: int = 30,
    horizon_hours: float = 40,
    min_first_minutes: int = 5,
) -> list[Slot]:
    """Build slots from now until the end of the price forecast.

    The first slot runs from now to the next slot boundary, so the plan always
    describes the present moment.
    """
    imports = _dedupe(imports)
    exports = _dedupe(exports)
    if not imports:
        return []
    horizon_end = min(imports[-1].end, now + timedelta(hours=horizon_hours))
    step = timedelta(minutes=slot_minutes)
    local_now = now.astimezone(tz)
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    boundary = midnight + step * (int((local_now - midnight) / step) + 1)
    if (boundary - local_now) < timedelta(minutes=min_first_minutes):
        boundary += step
    slots = []
    start = local_now
    while start < horizon_end:
        end = min(boundary, horizon_end)
        imp = average_over(imports, start, end)
        if imp is None:
            break
        exp = average_over(exports, start, end)
        sol = average_over(solar, start, end)
        mid = start + (end - start) / 2
        slots.append(
            Slot(
                start=start,
                hours=(end - start).total_seconds() / 3600,
                import_price=round(imp, 5),
                export_price=round(exp if exp is not None else 0.0, 5),
                solar_kw=round(max(sol or 0.0, 0.0), 3),
                load_kw=round(load_profile_kw(load_profile, mid.astimezone(tz)), 3),
            )
        )
        start, boundary = end, end + step
    return slots


def deadlines(now: datetime, tz, hour: int, minute: int, days: int = 3) -> list[datetime]:
    """Upcoming local deadline times (the start of the evening peak)."""
    local = now.astimezone(tz)
    out = []
    for d in range(days):
        dl = (local + timedelta(days=d)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dl > local:
            out.append(dl)
    return out


def profile_from_samples(samples: list[tuple[datetime, float]], tz, bins: int = 48, default: float = 1.0) -> list[float]:
    """Average (time, kW) samples into a time-of-day profile."""
    sums = [0.0] * bins
    counts = [0] * bins
    for when, kw in samples:
        local = when.astimezone(tz)
        idx = int((local.hour * 60 + local.minute) / (1440 / bins)) % bins
        sums[idx] += kw
        counts[idx] += 1
    overall = sum(sums) / sum(counts) if sum(counts) else default
    return [round(sums[i] / counts[i], 3) if counts[i] else round(overall, 3) for i in range(bins)]
