from datetime import datetime, timedelta

from custom_components.energy_planner.watchdog import ResponseTracker, Watchdog

from .helpers import BNE

T = datetime(2026, 10, 30, 12, 0, tzinfo=BNE)


def test_alerts_once_after_the_delay_and_reports_when_sorted():
    w = Watchdog()
    assert w.update(T, {"x": "stuck"}) == ([], [])
    assert w.update(T + timedelta(minutes=10), {"x": "stuck"}) == ([], [])
    assert w.update(T + timedelta(minutes=15), {"x": "stuck"}) == (["stuck"], [])
    assert w.blocked() == ["stuck"]
    assert w.update(T + timedelta(minutes=20), {"x": "stuck"}) == ([], [])
    assert w.update(T + timedelta(minutes=25), {}) == ([], ["stuck"])
    assert w.blocked() == []


def test_short_blips_never_alert():
    w = Watchdog()
    w.update(T, {"x": "stuck"})
    assert w.update(T + timedelta(minutes=5), {}) == ([], [])
    assert w.update(T + timedelta(minutes=20), {"x": "stuck"}) == ([], [])


def test_battery_not_following_a_charge():
    r = ResponseTracker()
    assert r.problem("charge", 10.0, T, 50.0, 20.0) is None
    # 10 kW for 10 min should add ~8%; 0.5% is not following.
    msg = r.problem("charge", 10.0, T + timedelta(minutes=10), 50.5, 20.0)
    assert msg and "charging at 10 kW" in msg
    r2 = ResponseTracker()
    r2.problem("charge", 10.0, T, 50.0, 20.0)
    assert r2.problem("charge", 10.0, T + timedelta(minutes=10), 57.0, 20.0) is None


def test_hold_that_drains_is_reported():
    r = ResponseTracker()
    r.problem("hold", 0.0, T, 60.0, 20.0)
    assert r.problem("hold", 0.0, T + timedelta(minutes=12), 55.0, 20.0)
    r2 = ResponseTracker()
    r2.problem("hold", 0.0, T, 60.0, 20.0)
    assert r2.problem("hold", 0.0, T + timedelta(minutes=12), 63.0, 20.0) is None


def test_full_battery_is_not_a_failed_charge():
    r = ResponseTracker()
    r.problem("charge", 5.0, T, 99.0, 20.0)
    assert r.problem("charge", 5.0, T + timedelta(minutes=15), 99.0, 20.0) is None
