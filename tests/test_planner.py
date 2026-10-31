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


def test_no_force_discharge_from_an_empty_battery():
    # Morning feed-in beats midday feed-in, which tempts the plan to "discharge"
    # at the reserve just to export solar instead of storing it.
    def prices(hr):
        if 5 <= hr < 10:
            return (0.15, 0.06)
        return (0.09, 0.03) if 10 <= hr < 15 else (0.18, 0.08)

    p = _params(battery_soc_pct=12, battery_reserve_pct=10)
    result = plan(day_slots(START, prices=prices), p)
    cap = p.battery_capacity_kwh
    for s in result.steps:
        if s.mode == MODE_DISCHARGE:
            assert -s.battery_kw >= s.power_kw * 0.5 - 1e-6, s


def _midday(dull_peak=4.9, typical_peak=None, import_now=0.102, n=40, prices=None):
    """From 11:45: solar falling away through the afternoon, peak price at 15:00."""
    from custom_components.energy_planner.planner import Slot

    start = datetime(2026, 10, 31, 11, 45, tzinfo=BNE)
    slots = []
    for i in range(n):
        t = start + timedelta(minutes=30 * i)
        hr = t.hour + t.minute / 60
        shape = max(0.0, 1 - ((hr - 12) / 6.5) ** 2) if 6 < hr < 18.5 else 0.0
        if prices:
            imp, exp = prices(hr)
        else:
            imp = import_now if i == 0 else (0.106 if hr < 15 or hr >= 21 else 0.185)
            exp = 0.048
        slots.append(
            Slot(
                t,
                0.5,
                imp,
                exp,
                round(dull_peak * shape, 2),
                1.2,
                None if typical_peak is None else round(typical_peak * shape, 2),
            )
        )
    return slots


TODAY_PEAK = datetime(2026, 10, 31, 15, 0, tzinfo=BNE)


def _car_today(**kw):
    base = dict(
        battery_soc_pct=100,
        battery_reserve_pct=0,
        ev_connected=True,
        ev_capacity_kwh=64.7,
        ev_soc_pct=65,
        ev_target_pct=80,
        ev_levels_kw=(1.44, 3.84, 7.68),
        ev_min_kw=1.44,
        deadlines=[TODAY_PEAK, TODAY_PEAK + timedelta(days=1)],
    )
    base.update(kw)
    return Params(**base)


def test_car_takes_spare_solar_before_the_grid():
    result = plan(_midday(dull_peak=9.0), _car_today())
    before = [s for s in result.steps if s.start < TODAY_PEAK]
    assert result.ev_soc_at_deadline >= 79.5
    assert all(s.grid_kw <= 0.01 for s in before)  # no grid at all
    assert before[0].ev_solar and before[0].ev_kw > 3


def test_waits_for_the_sun_when_a_typical_day_covers_the_car():
    # Today's case: on the cautious forecast alone the sun falls short, so the
    # plan bought at full power straight away. Hedged with the typical-day
    # forecast it follows the sun and leaves the top-up for later.
    result = plan(_midday(dull_peak=3.5, typical_peak=8.0, import_now=0.106), _car_today())
    first = result.steps[0]
    assert first.ev_solar and first.ev_level_kw == 0
    assert first.grid_kw <= 0.01
    # The plan shown is the dull day's, and it still makes 80% by 15:00.
    assert result.ev_soc_at_deadline >= 79.5
    assert any(s.grid_kw > 0.5 for s in result.steps if s.start < TODAY_PEAK)


def test_buys_early_only_what_a_dull_day_needs_anyway():
    # Grid is 0.4c cheaper now. The dull-day plan alone buys 7.7 kW now; the
    # hedged plan only buys what a typical day's sun would cover regardless.
    cautious = plan(_midday(dull_peak=3.5), _car_today()).steps[0]
    hedged = plan(_midday(dull_peak=3.5, typical_peak=8.0), _car_today()).steps[0]
    assert cautious.grid_kw > 5
    assert hedged.grid_kw < cautious.grid_kw / 2


def test_dull_day_still_buys_what_it_must_in_time():
    # Too little sun on either forecast: the top-up can't all be left to the end.
    result = plan(_midday(dull_peak=1.5, typical_peak=2.0), _car_today(ev_soc_pct=50))
    assert result.ev_soc_at_deadline >= 79.5


def test_negative_import_price_switches_the_panels_off():
    def prices(hr):
        return (-0.05, -0.08) if 12.5 <= hr < 14 else (0.106, 0.048)

    slots = _midday(dull_peak=6.0, prices=prices)
    result = plan(slots, _car_today(battery_soc_pct=60, can_curtail_solar=True))
    negative = [s for s in result.steps if s.import_price < 0]
    assert negative and negative[0].curtail and negative[0].solar_kw == 0
    for s in negative:
        # Panels off, unless the supply limit means switching them off wouldn't
        # buy any more from the grid.
        assert s.curtail or s.grid_kw >= 14 - s.solar_kw, s
        assert s.grid_kw > s.load_kw  # paid to import more than the house uses
    assert not any(s.curtail for s in result.steps if s.import_price >= 0)


def test_panels_stay_on_at_zero_or_without_control():
    def prices(hr):
        return (0.0, -0.02) if 12.5 <= hr < 14 else (0.106, 0.048)

    zero = plan(_midday(dull_peak=6.0, prices=prices), _car_today(battery_soc_pct=60, can_curtail_solar=True))
    assert not any(s.curtail for s in zero.steps)

    def negative(hr):
        return (-0.05, -0.08) if 12.5 <= hr < 14 else (0.106, 0.048)

    off = plan(_midday(dull_peak=6.0, prices=negative), _car_today(battery_soc_pct=60))
    assert not any(s.curtail for s in off.steps)


def test_keeps_the_car_charging_unless_stopping_saves_real_money():
    # 3.9 kWh to go and near-equal prices: the plan would happily move the
    # charge around, but once the car is charging it carries on.
    def prices(hr):
        return (0.105 if hr < 14 else 0.104, 0.048) if hr < 15 else (0.19, 0.08)

    slots = _midday(dull_peak=0.5, prices=prices)
    free = plan(slots, _car_today(ev_soc_pct=74)).steps[0]
    running = plan(slots, _car_today(ev_soc_pct=74, ev_level_now_kw=7.68)).steps[0]
    assert free.ev_kw < 0.05  # left for the 0.1c cheaper hour
    assert running.ev_level_kw == 7.68 and running.ev_kw > 7

    # A real saving still stops it.
    def dear_now(hr):
        return (0.30 if hr < 12.5 else 0.10, 0.048) if hr < 15 else (0.19, 0.08)

    stopped = plan(_midday(dull_peak=0.5, prices=dear_now), _car_today(ev_soc_pct=74, ev_level_now_kw=7.68)).steps[0]
    assert stopped.ev_kw < 0.05
