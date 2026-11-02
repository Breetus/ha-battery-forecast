from datetime import datetime, timedelta

from custom_components.energy_planner.control import (
    BatteryState,
    EvState,
    LastCommand,
    decide_battery,
    decide_ev,
)
from custom_components.energy_planner.planner import Step

from .helpers import BNE

NOW = datetime(2026, 10, 30, 2, 0, tzinfo=BNE)


def _step(mode="normal", power=0.0, ev=0.0):
    return Step(NOW, 0.5, mode, power, 0, ev, 0, 0, 1, 0.2, 0.05, 0, 50, None)


def _state(mode="normal", remaining=None, hold=False, ev=False):
    return BatteryState(mode, remaining, hold, ev)


def test_starts_force_charge():
    d = decide_battery(_step("charge", 5.0), _state(), None, NOW)
    assert d.commands[0].service == "force_charge"
    assert d.commands[0].data == {"duration": "30", "power_w": 5000}


def test_leaves_running_charge_alone_until_it_nears_expiry():
    last = LastCommand("charge", 5.0, NOW - timedelta(minutes=10))
    assert decide_battery(_step("charge", 5.0), _state("force_charge", 20), last, NOW).commands == []
    refresh = decide_battery(_step("charge", 5.0), _state("force_charge", 8), last, NOW)
    assert refresh.commands[0].service == "force_charge"


def test_power_change_recommands():
    last = LastCommand("charge", 5.0, NOW - timedelta(minutes=10))
    d = decide_battery(_step("charge", 10.0), _state("force_charge", 20), last, NOW)
    assert d.commands[0].data["power_w"] == 10000


def test_restores_normal_after_our_command():
    last = LastCommand("hold", 0, NOW - timedelta(minutes=10))
    d = decide_battery(_step("normal"), _state("hold_soc", 20), last, NOW)
    assert d.commands[0].service == "restore_normal"


def test_resends_restore_when_the_inverter_stays_forced():
    # PowerSync says normal, but its restore left the inverter forced and stopped.
    last = LastCommand("normal", 0, NOW - timedelta(minutes=30))
    state = BatteryState("normal", None, False, False, soc_pct=50, inverter_forced=True, ems_forced=True)
    d = decide_battery(_step("normal"), state, last, NOW)
    assert [c.service for c in d.commands] == ["restore_normal"]
    assert "stuck" in d.status
    # Not again while PowerSync is still applying it.
    just_sent = LastCommand("normal", 0, NOW - timedelta(seconds=30))
    assert decide_battery(_step("normal"), state, just_sent, NOW).commands == []


def test_puts_a_forced_inverter_back_under_the_car_hold():
    # The EV rule's hold only caps discharge; something outside HA left the
    # inverter forced and stopped, so the battery can't take the spare solar.
    state = BatteryState("hold_soc", 20, True, True, soc_pct=21, inverter_forced=True, ems_forced=True)
    d = decide_battery(_step("normal"), state, None, NOW)
    assert [c.service for c in d.commands] == ["restore_normal", "hold_battery_soc"]
    assert d.mode == "hold" and "stuck" in d.status
    # Not again while PowerSync is still applying it.
    just_sent = LastCommand("hold", 0, NOW - timedelta(seconds=30))
    assert decide_battery(_step("normal"), state, just_sent, NOW).commands == []
    # A clean hold is left alone.
    clean = BatteryState("hold_soc", 20, True, True, soc_pct=21)
    assert decide_battery(_step("normal"), clean, None, NOW).commands == []
    # PowerSync already back to normal: just the restore, the EV rule holds again.
    normal = BatteryState("normal", None, True, True, soc_pct=21, inverter_forced=True, ems_forced=True)
    assert [c.service for c in decide_battery(_step("normal"), normal, None, NOW).commands] == ["restore_normal"]


def test_puts_a_forced_inverter_back_under_our_own_hold():
    last = LastCommand("hold", 0, NOW - timedelta(minutes=10))
    state = BatteryState("hold_soc", 25, False, False, soc_pct=40, inverter_forced=True, ems_forced=True)
    d = decide_battery(_step("hold"), state, last, NOW)
    assert [c.service for c in d.commands] == ["restore_normal", "hold_battery_soc"]
    # A new hold from a forced inverter restores it first too.
    state = BatteryState("normal", None, False, True, soc_pct=40, ems_forced=True)
    d = decide_battery(_step("normal"), state, None, NOW)
    assert [c.service for c in d.commands] == ["restore_normal", "hold_battery_soc"]


def test_restores_normal_before_a_sale_from_a_leftover_forced_mode():
    state = BatteryState("normal", None, False, False, soc_pct=50, ems_forced=True)
    d = decide_battery(_step("discharge", 5.0), state, None, NOW)
    assert [c.service for c in d.commands] == ["restore_normal", "force_discharge"]
    # A clean inverter just gets the sale.
    d = decide_battery(_step("discharge", 5.0), _state(), None, NOW)
    assert [c.service for c in d.commands] == ["force_discharge"]


def test_an_expired_powersync_hold_is_not_an_override():
    # PowerSync's own hold ran out long ago but it still shows hold_soc.
    stale = BatteryState("hold_soc", 0, False, False, soc_pct=40, expired=True)
    d = decide_battery(_step("charge", 10.0), stale, None, NOW)
    assert not d.override
    assert [c.service for c in d.commands] == ["force_charge"]
    d = decide_battery(_step("normal"), stale, None, NOW)
    assert [c.service for c in d.commands] == ["restore_normal"]
    # While it still has time left, it's someone else's.
    live = BatteryState("hold_soc", 20, False, False, soc_pct=40)
    assert decide_battery(_step("charge", 10.0), live, None, NOW).override


def test_a_sale_stops_at_the_selling_floor():
    last = LastCommand("discharge", 10.0, NOW - timedelta(minutes=5))
    state = BatteryState("force_discharge", 25, False, False, soc_pct=39.5, sell_floor_pct=40)
    d = decide_battery(_step("discharge", 10.0), state, last, NOW)
    assert [c.service for c in d.commands] == ["restore_normal"]
    assert "floor" in d.status.lower()
    # Above the floor the sale carries on.
    state = BatteryState("force_discharge", 25, False, False, soc_pct=45, sell_floor_pct=40)
    assert decide_battery(_step("discharge", 10.0), state, last, NOW).commands == []


def test_manual_command_takes_over():
    last = LastCommand("normal", 0, NOW - timedelta(minutes=10))
    d = decide_battery(_step("charge", 5.0), _state("force_discharge", 40), last, NOW)
    assert d.override and d.commands == []


def test_house_ev_hold_is_not_an_override():
    d = decide_battery(_step("normal"), _state("hold_soc", 20, hold=True, ev=True), None, NOW)
    assert not d.override and d.commands == []


def test_holds_battery_while_car_charges_after_our_force_charge():
    # Our force charge ran while the car charged; the house rule let go of it.
    last = LastCommand("charge", 5.0, NOW - timedelta(minutes=20))
    d = decide_battery(_step("normal"), _state("force_charge", 10, ev=True), last, NOW)
    assert d.commands[0].service == "hold_battery_soc"
    assert "car" in d.status


def test_keeps_its_own_hold_while_car_charges():
    last = LastCommand("hold", 0, NOW - timedelta(minutes=25))
    d = decide_battery(_step("normal"), _state("hold_soc", 5, ev=True), last, NOW)
    assert d.commands[0].service == "hold_battery_soc"  # refreshed before it lapses


def test_waits_for_powersync_to_confirm():
    last = LastCommand("charge", 5.0, NOW - timedelta(seconds=30))
    d = decide_battery(_step("charge", 5.0), _state("normal"), last, NOW)
    assert d.commands == []


def test_retries_unconfirmed_command():
    last = LastCommand("charge", 5.0, NOW - timedelta(minutes=5))
    d = decide_battery(_step("charge", 5.0), _state("normal"), last, NOW)
    assert d.commands[0].service == "force_charge"


def test_ev_on_with_current():
    d = decide_ev(_step(ev=3.7), EvState(True, False, 32))
    assert d.switch_on is True and d.current_a == 16


def test_ev_off_when_not_planned():
    d = decide_ev(_step(ev=0), EvState(True, True, 16))
    assert d.switch_on is False


def test_ev_untouched_when_unplugged():
    d = decide_ev(_step(ev=7.4), EvState(False, False, 16))
    assert d.switch_on is None and d.current_a is None


def test_holds_at_the_reserve_instead_of_draining_to_the_inverter_minimum():
    state = BatteryState("normal", None, False, False, soc_pct=10.2, reserve_pct=10)
    d = decide_battery(_step("normal"), state, None, NOW)
    assert d.commands[0].service == "hold_battery_soc"
    assert d.mode == "hold" and "reserve" in d.status


def test_reserve_hold_releases_with_hysteresis():
    last = LastCommand("hold", 0, NOW - timedelta(minutes=10))
    # 11% is still inside the release margin: keep holding.
    d = decide_battery(_step("normal"), BatteryState("hold_soc", 20, False, False, 11.0, 10), last, NOW)
    assert d.commands == []
    # 12.5% is clear of it: back to normal.
    d = decide_battery(_step("normal"), BatteryState("hold_soc", 20, False, False, 12.5, 10), last, NOW)
    assert d.commands[0].service == "restore_normal"


def test_normal_above_reserve_is_untouched():
    d = decide_battery(_step("normal"), BatteryState("normal", None, False, False, 40.0, 10), None, NOW)
    assert d.commands == []


def test_sale_stops_at_the_reserve():
    last = LastCommand("discharge", 10.0, NOW - timedelta(minutes=3))
    state = BatteryState("force_discharge", 25, False, False, soc_pct=9.8, reserve_pct=10)
    d = decide_battery(_step("discharge", 10.0), state, last, NOW)
    assert d.commands[0].service == "hold_battery_soc"


def test_no_reserve_hold_when_reserve_is_zero():
    d = decide_battery(_step("normal"), BatteryState("normal", None, False, False, soc_pct=0.0, reserve_pct=0), None, NOW)
    assert d.commands == []


def _solar_step(level=0.0, planned=2.0):
    step = _step(ev=planned)
    step.ev_solar = level == 0
    step.ev_level_kw = level
    return step


def test_follows_spare_solar():
    d = decide_ev(_solar_step(), EvState(True, True, 6, spare_kw=3.0), voltage=240)
    assert d.current_a == 11 and d.switch_on is None  # (3.0 - 0.2) kW / 240 V
    assert "spare solar" in d.status


def test_starts_on_spare_solar_after_a_rest():
    rested = decide_ev(_solar_step(), EvState(True, False, 6, spare_kw=2.5, off_for_s=600), voltage=240)
    assert rested.switch_on is True and rested.current_a == 9
    too_soon = decide_ev(_solar_step(), EvState(True, False, 6, spare_kw=2.5, off_for_s=60), voltage=240)
    assert too_soon.switch_on is None and too_soon.current_a is None


def test_rides_out_a_cloud_then_stops():
    cloud = decide_ev(_solar_step(), EvState(True, True, 10, spare_kw=0.5, spare_short_for_s=60), voltage=240)
    assert cloud.current_a == 6 and cloud.switch_on is None
    stop = decide_ev(_solar_step(), EvState(True, True, 6, spare_kw=0.5, spare_short_for_s=200), voltage=240)
    assert stop.switch_on is False


def test_fixed_level_takes_extra_spare_solar():
    more = decide_ev(_solar_step(level=3.84), EvState(True, True, 16, spare_kw=6.0), voltage=240)
    assert more.current_a == 24 and more.status.endswith("spare solar")
    floor = decide_ev(_solar_step(level=3.84), EvState(True, True, 24, spare_kw=1.0), voltage=240)
    assert floor.current_a == 16 and floor.status.endswith("spare solar and grid")


def test_spare_unknown_falls_back_to_the_plan():
    d = decide_ev(_solar_step(planned=2.4), EvState(True, False, 6), voltage=240)
    assert d.switch_on is True and d.current_a == 10


def test_no_ride_out_once_the_sun_has_gone():
    dusk = decide_ev(_solar_step(), EvState(True, True, 10, spare_kw=0.0, sun_kw=0.1, spare_short_for_s=10), voltage=240)
    assert dusk.switch_on is False
