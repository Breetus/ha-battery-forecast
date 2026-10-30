"""Tracks this month's highest half-hour grid import inside the demand window.

No Home Assistant imports.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

BUCKET = timedelta(minutes=30)
# A half hour counts only if readings cover most of it.
MIN_COVERAGE = 0.8
# Readings further apart than this are a gap, not a steady value.
MAX_GAP = timedelta(minutes=10)


def _bucket(when: datetime) -> datetime:
    return when.replace(minute=(when.minute // 30) * 30, second=0, microsecond=0)


class DemandTracker:
    def __init__(self, month: str | None = None, peak_kw: float = 0.0, peak_at: str | None = None) -> None:
        self.month = month
        self.peak_kw = peak_kw
        self.peak_at = peak_at
        self._bucket: datetime | None = None
        self._kwh = 0.0
        self._seconds = 0.0
        self._last: tuple[datetime, float] | None = None

    @staticmethod
    def month_key(when: datetime) -> str:
        return f"{when.year:04d}-{when.month:02d}"

    def sample(self, when: datetime, import_kw: float, in_window: Callable[[datetime], bool]) -> bool:
        """Add a grid reading (local time, kW, + import). Returns True if the peak rose."""
        changed = False
        if self._last is not None:
            prev_t, prev_kw = self._last
            if timedelta(0) < when - prev_t <= MAX_GAP:
                t = prev_t
                while t < when:
                    b = _bucket(t)
                    if self._bucket != b:
                        changed |= self._close(in_window)
                        self._bucket = b
                    seg_end = min(when, b + BUCKET)
                    secs = (seg_end - t).total_seconds()
                    self._kwh += max(prev_kw, 0.0) * secs / 3600
                    self._seconds += secs
                    t = seg_end
            elif when - prev_t > MAX_GAP:
                changed |= self._close(in_window)
                self._bucket = None
        self._last = (when, import_kw)
        return changed

    def _close(self, in_window: Callable[[datetime], bool]) -> bool:
        changed = False
        b = self._bucket
        if b is not None and self._seconds >= BUCKET.total_seconds() * MIN_COVERAGE and in_window(b):
            avg = self._kwh / (self._seconds / 3600)
            key = self.month_key(b)
            if self.month != key:
                self.month, self.peak_kw, self.peak_at = key, 0.0, None
                changed = True
            if avg > self.peak_kw:
                self.peak_kw = round(avg, 3)
                self.peak_at = b.isoformat()
                changed = True
        self._kwh = 0.0
        self._seconds = 0.0
        return changed

    def current_peak(self, now: datetime) -> float:
        """The peak that already counts this month (0 after the month rolls over)."""
        return self.peak_kw if self.month == self.month_key(now) else 0.0

    def as_dict(self) -> dict:
        return {"month": self.month, "peak_kw": self.peak_kw, "peak_at": self.peak_at}
