from datetime import datetime, timedelta

import time

from custom_components.energy_planner.planner import (
    MODE_CHARGE,
    MODE_DISCHARGE,
    Params,
    plan,
)

from .helpers import BNE, day_slots

START = datetime(2026, 10, 30, 20, 0, tzinfo=BNE)
PEAK = datetime(2026, 10, 31, 16, 0, tzinfo=BNE)
PEAK2 = PEAK + timedelta(days=1)


def _params(**kw):
    base = dict(battery_soc_pct=40, deadlines=[PEAK, PEAK2])
    base.update(kw)
    return Params(**base)


def test_sunny_day_reaches_targets_without_grid_charging():
    p = _params(ev_connected=True, ev_soc_pct=40, ev_target_pct=80)
    result = plan(day_slots(START), p)
    assert result.battery_soc_at_deadline >= 79.5
    assert result.ev_soc_at_deadline >= 79.5
    before_peak = [s for s in result.steps if s.start < PEAK]
    assert not any(s.mode == MODE_CHARGE for s in before_peak)


def test_cloudy_day_charges_in_the_cheapest_slots():
    p = _params()
    result = plan(day_slots(START, solar_peak=1.0), p)
    assert result.battery_soc_at_deadline >= 79.5
    charging = [s for s in result.steps if s.mode == MODE_CHARGE and s.start < PEAK]
    assert charging
    # The 5c midday window is the cheapest before the peak.
    assert all(s.import_price <= 0.05 for s in charging)


def test_sells_into_the_peak_when_it_pays():
    p = _params(battery_soc_pct=90)
    result = plan(day_slots(START), p)
    selling = [s for s in result.steps if s.mode == MODE_DISCHARGE]
    assert selling
    assert all(s.export_price >= 0.30 for s in selling)


def test_no_selling_when_switched_off():
    p = _params(battery_soc_pct=90, allow_export=False)
    result = plan(day_slots(START), p)
    assert not any(s.mode == MODE_DISCHARGE for s in result.steps)


def test_does_not_sell_when_spread_is_below_wear_cost():
    flat = lambda hr: (0.25, 0.22)  # noqa: E731
    p = _params(battery_soc_pct=90, wear_cost=0.05)
    result = plan(day_slots(START, prices=flat), p)
    assert not any(s.mode == MODE_DISCHARGE for s in result.steps)


def test_never_sells_below_target_before_the_peak():
    # A juicy midday export price, but the battery must still be at 80% at 16:00
    # and grid charging is not allowed.
    def prices(hr):
        return (0.30, 1.00) if 9 <= hr < 10 else (0.30, 0.05)

    p = _params(battery_soc_pct=85, allow_grid_charge=False)
    result = plan(day_slots(START, solar_peak=0.0, prices=prices), p)
    assert result.battery_soc_at_deadline >= 79.5


def test_grid_import_limit_respected():
    p = _params(battery_soc_pct=10, ev_connected=True, ev_soc_pct=20, ev_target_pct=80, import_limit_kw=12)
    result = plan(day_slots(START, solar_peak=0.0), p)
    assert max(s.grid_kw for s in result.steps) <= 12.01


def test_battery_never_feeds_the_car():
    p = _params(battery_soc_pct=90, ev_connected=True, ev_soc_pct=20, ev_target_pct=80)
    result = plan(day_slots(START), p)
    for s in result.steps:
        if s.ev_kw > 0:
            assert s.battery_kw >= -1e-6


def test_charges_when_import_price_is_negative():
    def prices(hr):
        return (-0.10, -0.12) if 11 <= hr < 13 else (0.30, 0.05)

    p = _params(battery_soc_pct=50)
    result = plan(day_slots(START, solar_peak=2.0, prices=prices), p)
    negative = [s for s in result.steps if s.import_price < 0 and s.battery_soc_pct < 100]
    assert all(s.battery_kw > 0 for s in negative)


def test_fast_enough_for_a_raspberry_pi():
    p = _params(ev_connected=True, ev_soc_pct=10, ev_target_pct=90)
    t0 = time.perf_counter()
    plan(day_slots(START, n=80), p)
    assert time.perf_counter() - t0 < 3.0


def test_demand_window_is_covered_by_the_battery():
    # Flat 18c import and 8c feed-in all day (like a demand tariff), no sun.
    flat = lambda hr: (0.18, 0.08)  # noqa: E731
    slots = day_slots(START, solar_peak=0.0, load=1.5, prices=flat)
    for s in slots:
        s.demand = 16 <= s.start.hour < 21
    p = _params(battery_soc_pct=30, demand_rate=10.0, demand_peak_kw=0.0)
    result = plan(slots, p)
    window = [s for s in result.steps if 16 <= s.start.hour < 21]
    assert window and max(s.grid_kw for s in window) <= 0.01
    # Without a demand charge, a flat price gives no reason to pre-charge.
    plain = plan(day_slots(START, solar_peak=0.0, load=1.5, prices=flat), _params(battery_soc_pct=30, battery_target_pct=0))
    assert not any(s.mode == MODE_CHARGE for s in plain.steps)


def test_demand_charge_spares_import_below_this_months_peak():
    flat = lambda hr: (0.18, 0.08)  # noqa: E731
    slots = day_slots(START, solar_peak=0.0, load=1.5, prices=flat)
    for s in slots:
        s.demand = 16 <= s.start.hour < 21
    # The month already has a 3 kW peak, so importing 1.5 kW in the window is free of demand charge.
    p = _params(battery_soc_pct=10, battery_reserve_pct=10, battery_target_pct=10, demand_rate=10.0, demand_peak_kw=3.0)
    result = plan(slots, p)
    assert not any(s.mode == MODE_CHARGE for s in result.steps)
