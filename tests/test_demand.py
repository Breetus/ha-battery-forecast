from datetime import datetime, timedelta

from custom_components.energy_planner import forecast as fc
from custom_components.energy_planner.demand import DemandTracker

from .helpers import BNE


def window(local):
    return fc.in_window(local, (16, 0), (21, 0))


def test_tracks_half_hour_average_in_window_only():
    t = datetime(2026, 10, 30, 15, 0, tzinfo=BNE)
    d = DemandTracker()
    # 9 kW until 16:05 (outside the window until 16:00), 2 kW to 16:20, then 4 kW.
    for i in range(0, 61, 5):
        d.sample(t + timedelta(minutes=i), 9.0, window)
    for i in range(65, 76, 5):
        d.sample(t + timedelta(minutes=i), 2.0, window)
    for i in range(80, 106, 5):
        d.sample(t + timedelta(minutes=i), 4.0, window)
    # 16:00-16:30: 5 min at 9, 15 min at 2, 10 min at 4 = 3.83 kW
    assert abs(d.peak_kw - (5 * 9 + 15 * 2 + 10 * 4) / 30) < 0.01
    assert d.peak_at.startswith("2026-10-30T16:00")


def test_new_month_resets_the_peak():
    d = DemandTracker("2026-10", 5.0)
    assert d.current_peak(datetime(2026, 11, 1, 1, 0, tzinfo=BNE)) == 0.0
    assert d.current_peak(datetime(2026, 10, 31, 1, 0, tzinfo=BNE)) == 5.0


def test_gaps_do_not_count():
    t = datetime(2026, 10, 30, 16, 0, tzinfo=BNE)
    d = DemandTracker()
    d.sample(t, 8.0, window)
    d.sample(t + timedelta(minutes=29), 8.0, window)  # one 29-minute gap: not a reading
    d.sample(t + timedelta(minutes=31), 0.0, window)
    assert d.peak_kw == 0.0


def test_in_window_wraps_midnight():
    assert fc.in_window(datetime(2026, 10, 30, 23, 0), (22, 0), (6, 0))
    assert not fc.in_window(datetime(2026, 10, 30, 12, 0), (22, 0), (6, 0))


def test_parse_amber_demand_flags():
    flags = fc.parse_amber_demand(
        [
            {"channelType": "general", "startTime": "2026-10-30T06:00:01Z", "endTime": "2026-10-30T06:30:00Z", "tariffInformation": {"demandWindow": True}},
            {"channelType": "general", "startTime": "2026-10-30T05:30:01Z", "endTime": "2026-10-30T06:00:00Z", "tariffInformation": {"demandWindow": False}},
        ]
    )
    assert len(flags) == 1 and flags[0].start.hour == 6
