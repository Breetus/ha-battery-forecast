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


def test_manual_command_takes_over():
    last = LastCommand("normal", 0, NOW - timedelta(minutes=10))
    d = decide_battery(_step("charge", 5.0), _state("force_discharge", 40), last, NOW)
    assert d.override and d.commands == []


def test_house_ev_hold_is_not_an_override():
    d = decide_battery(_step("normal"), _state("hold_soc", 20, hold=True, ev=True), None, NOW)
    assert not d.override and d.commands == []


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
