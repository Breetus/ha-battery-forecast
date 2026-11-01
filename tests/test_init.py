"""End-to-end: set the integration up inside a test Home Assistant."""
from datetime import timedelta, timezone
from types import SimpleNamespace

import pytest

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed, async_mock_service

from custom_components.energy_planner.const import DOMAIN


@pytest.fixture(autouse=True)
def _env(recorder_mock, enable_custom_integrations):
    yield


def _amber_forecast(now):
    start = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
    out = []
    for i in range(80):
        t = start + timedelta(minutes=30 * i)
        cents = 2.0 if i < 2 else 45.0  # cheap for under an hour, then dear
        for channel, value in (("general", cents), ("feedIn", -3.0)):
            out.append(
                {
                    "type": "ForecastInterval",
                    "channelType": channel,
                    "startTime": (t + timedelta(seconds=1)).astimezone(timezone.utc).isoformat(),
                    "endTime": (t + timedelta(minutes=30)).astimezone(timezone.utc).isoformat(),
                    "perKwh": value,
                }
            )
    return out


async def _setup(hass, powersync_prices=True):
    now = dt_util.utcnow()
    ps_entry = MockConfigEntry(domain="power_sync", title="PowerSync")
    ps_entry.add_to_hass(hass)
    hass.data["power_sync"] = {
        ps_entry.entry_id: {
            "amber_coordinator": SimpleNamespace(
                data={"forecast": _amber_forecast(now), "current": []} if powersync_prices else None
            ),
            "solcast_coordinator": SimpleNamespace(data={"forecasts": []}),
        }
    }
    if not powersync_prices:
        # Amber Express shape: {time, value} in $/kWh.
        general = [i for i in _amber_forecast(now) if i["channelType"] == "general"]
        feed_in = [i for i in _amber_forecast(now) if i["channelType"] == "feedIn"]
        hass.states.async_set(
            "sensor.amber_express_home_general_price",
            "0.02",
            {"forecast": [{"time": i["startTime"], "value": i["perKwh"] / 100} for i in general]},
        )
        hass.states.async_set(
            "sensor.amber_express_home_feed_in_price",
            "-0.03",
            {"forecast": [{"time": i["startTime"], "value": i["perKwh"] / 100} for i in feed_in]},
        )
        hass.states.async_set("sensor.power_sync_current_export_price", "0.03")
    hass.states.async_set("sensor.power_sync_battery_level", "30", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.power_sync_battery_mode", "normal")
    hass.states.async_set("sensor.power_sync_home_load", "1.0", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.ev_charging_power", "0", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.charger_status_connector", "Available")
    hass.states.async_set("switch.charger_charge_control", "off")
    hass.states.async_set("number.charger_maximum_current", "32")
    calls = {name: async_mock_service(hass, "power_sync", name) for name in ("force_charge", "force_discharge", "hold_battery_soc", "restore_normal")}
    entry = MockConfigEntry(domain=DOMAIN, data={}, title="Energy Planner")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, calls


async def test_plan_only_does_not_command(hass):
    _, calls = await _setup(hass)
    plan = hass.states.get("sensor.energy_planner_plan_now")
    assert plan is not None and plan.state.startswith("Charge battery")
    assert len(plan.attributes["steps"]) > 40
    assert hass.states.get("select.energy_planner_mode").state == "Plan only"
    assert hass.states.get("sensor.energy_planner_status").state.startswith("Plan only")
    assert not any(c for c in calls.values())


async def test_auto_force_charges_in_cheap_window(hass):
    _, calls = await _setup(hass)
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.energy_planner_mode", "option": "Auto"}, blocking=True
    )
    await hass.async_block_till_done()
    assert len(calls["force_charge"]) == 1
    data = calls["force_charge"][0].data
    assert data["duration"] == "30" and data["power_w"] >= 2500
    # A second refresh while PowerSync reports the charge running sends nothing new.
    hass.states.async_set("sensor.power_sync_battery_mode", "force_charge", {"remaining_minutes": 29})
    await hass.async_block_till_done()
    assert len(calls["force_charge"]) == 1


async def test_manual_powersync_command_pauses_auto(hass):
    _, calls = await _setup(hass)
    hass.states.async_set("sensor.power_sync_battery_mode", "force_discharge", {"remaining_minutes": 60})
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.energy_planner_mode", "option": "Auto"}, blocking=True
    )
    await hass.async_block_till_done()
    assert not calls["force_charge"]
    assert hass.states.get("sensor.energy_planner_status").state.startswith("Manual")


async def test_settings_are_entities(hass):
    await _setup(hass)
    for entity_id in (
        "number.energy_planner_battery_target_at_peak",
        "number.energy_planner_ev_target_at_peak",
        "number.energy_planner_ev_state_of_charge_manual",
        "switch.energy_planner_allow_selling_to_grid",
        "time.energy_planner_peak_price_starts",
        "sensor.energy_planner_battery_at_peak",
        "sensor.energy_planner_next_change",
        "select.energy_planner_solar_forecast",
    ):
        assert hass.states.get(entity_id) is not None, entity_id


async def test_auto_charges_plugged_in_car(hass):
    _, calls = await _setup(hass)
    # Put the peak almost a day away, so the car can reach its target whatever the time now.
    an_hour_ago = dt_util.now() - timedelta(hours=1)
    await hass.services.async_call(
        "time",
        "set_value",
        {"entity_id": "time.energy_planner_peak_price_starts", "time": an_hour_ago.strftime("%H:%M:00")},
        blocking=True,
    )
    switch_on = async_mock_service(hass, "switch", "turn_on")
    set_current = async_mock_service(hass, "number", "set_value")
    hass.states.async_set("sensor.charger_status_connector", "Preparing")
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.energy_planner_mode", "option": "Auto"}, blocking=True
    )
    await hass.async_block_till_done()
    # Refresh requests within 10 s of each other are debounced.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=15))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.energy_planner_ev_at_peak").state not in (None, "unknown")
    assert float(hass.states.get("sensor.energy_planner_ev_at_peak").state) >= 79.5
    assert switch_on and switch_on[0].data["entity_id"] == "switch.charger_charge_control"
    # The charger already offers 32 A, which is what the plan wants.
    assert not set_current


async def test_falls_back_to_amber_express(hass):
    await _setup(hass, powersync_prices=False)
    status = hass.states.get("sensor.energy_planner_status")
    assert status.attributes["price_source"] == "price entities"
    steps = hass.states.get("sensor.energy_planner_plan_now").attributes["steps"]
    assert abs(steps[0]["import_price"] - 0.02) < 1e-6
    # Feed-in was negative-for-earning in this feed; it's flipped to match PowerSync.
    assert abs(steps[0]["export_price"] - 0.03) < 1e-6


async def test_uses_car_soc_and_charge_limit(hass):
    entry, _ = await _setup(hass)
    hass.states.async_set("sensor.wmw52ga0507r62550_battery_hv_state_of_charge", "70")
    hass.states.async_set("sensor.wmw52ga0507r62550_battery_ev_target_state_of_charge", "75")
    hass.states.async_set("sensor.charger_status_connector", "Preparing")
    await hass.data[DOMAIN][entry.entry_id].async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.energy_planner_status").attributes["ev_soc_used"] == 70
    assert float(hass.states.get("sensor.energy_planner_ev_at_peak").state) <= 75.5


async def test_alerts_phone_when_plan_is_blocked(hass, freezer):
    entry, calls = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    notify = async_mock_service(hass, "notify", "mobile_app_poco_x3_gt")
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.energy_planner_mode", "option": "Auto"}, blocking=True
    )
    await hass.async_block_till_done()
    assert calls["force_charge"]
    # Something else forces a discharge and keeps it there.
    hass.states.async_set("sensor.power_sync_battery_mode", "force_discharge", {"remaining_minutes": 60})
    await hass.async_block_till_done()
    for _ in range(4):
        freezer.tick(timedelta(minutes=5, seconds=10))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert len(notify) == 1
    assert "force_discharge" in notify[0].data["message"]
    assert hass.states.get("binary_sensor.energy_planner_plan_blocked").state == "on"
    # It clears, and says so once.
    hass.states.async_set("sensor.power_sync_battery_mode", "normal")
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=5, seconds=10))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(notify) == 2 and notify[1].data["message"].startswith("Sorted now")
    assert hass.states.get("binary_sensor.energy_planner_plan_blocked").state == "off"


async def test_no_alerts_in_plan_only(hass, freezer):
    entry, _ = await _setup(hass)
    notify = async_mock_service(hass, "notify", "mobile_app_poco_x3_gt")
    hass.states.async_set("sensor.power_sync_battery_mode", "force_discharge", {"remaining_minutes": 60})
    for _ in range(5):
        freezer.tick(timedelta(minutes=5, seconds=10))
        await hass.data[DOMAIN][entry.entry_id].async_refresh()
        await hass.async_block_till_done()
    assert not notify


async def test_sells_into_a_live_price_spike(hass):
    entry, calls = await _setup(hass)
    hass.states.async_set("sensor.power_sync_battery_level", "90", {"unit_of_measurement": "%"})
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.energy_planner_mode", "option": "Auto"}, blocking=True
    )
    await hass.async_block_till_done()
    # An unforecast 5-minute spike: feed-in pays $10/kWh right now.
    now = dt_util.utcnow()
    start = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
    spike = {
        "type": "CurrentInterval",
        "channelType": "feedIn",
        "startTime": (start + timedelta(seconds=1)).isoformat(),
        "endTime": (start + timedelta(minutes=5)).isoformat(),
        "perKwh": -1000.0,
    }
    ps = hass.data["power_sync"][next(iter(hass.data["power_sync"]))]["amber_coordinator"]
    ps.data = {**ps.data, "current": [spike]}
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=15))  # past the refresh cooldown
    hass.states.async_set("sensor.power_sync_current_export_price", "10.0")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()
    plan_now = hass.states.get("sensor.energy_planner_plan_now")
    assert plan_now.state.startswith("Sell"), plan_now.state
    assert calls["force_discharge"]


async def test_charger_follows_spare_solar(hass, freezer):
    from custom_components.energy_planner.planner import Plan, Step

    entry, _ = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    switch_on = async_mock_service(hass, "switch", "turn_on")
    switch_off = async_mock_service(hass, "switch", "turn_off")
    set_current = async_mock_service(hass, "number", "set_value")
    hass.states.async_set("sensor.charger_status_connector", "Preparing")
    hass.states.async_set("sensor.power_sync_grid_power", "-3000", {"unit_of_measurement": "W"})
    freezer.tick(timedelta(minutes=10))  # the charger has been off a while
    step = Step(dt_util.now(), 0.5, "normal", 0, 0, 2.0, 0, 3.0, 1.0, 0.1, 0.05, 0, 100, 66, ev_solar=True)
    coordinator.data["plan"] = Plan([step], 0, None, None, None)
    coordinator.settings["mode"] = "Auto"

    await coordinator._follow_solar()
    assert switch_on and set_current[-1].data["value"] == 11  # (3.0 - 0.2) kW at 240 V
    hass.states.async_set("switch.charger_charge_control", "on")
    hass.states.async_set("number.charger_maximum_current", "11")

    # A cloud: the car's 2.6 kW now needs 1.5 kW from the grid.
    hass.states.async_set("sensor.ev_charging_power", "2640", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.power_sync_grid_power", "1500", {"unit_of_measurement": "W"})
    await coordinator._follow_solar()
    assert set_current[-1].data["value"] == 6 and not switch_off  # rides it out at the minimum
    freezer.tick(timedelta(minutes=4))
    await coordinator._follow_solar()
    assert switch_off


async def test_plans_on_the_inverters_own_export_limit(hass):
    entry, _ = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    hass.states.async_set("sensor.power_sync_sungrow_diagnostics", "ok", {"export_limit_w": 5000, "export_limit_enabled": True})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.energy_planner_status").attributes["export_limit_kw"] == 5.0
    # 50 W is PowerSync curtailing a negative feed-in, not the inverter's limit.
    hass.states.async_set("sensor.power_sync_sungrow_diagnostics", "ok", {"export_limit_w": 50, "export_limit_enabled": True})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.energy_planner_status").attributes["export_limit_kw"] == 10.0


async def test_charging_soc_shows_only_during_the_planners_charge(hass):
    _, calls = await _setup(hass)
    assert hass.states.get("sensor.energy_planner_battery_while_planner_charges").state == "unavailable"
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.energy_planner_mode", "option": "Auto"}, blocking=True
    )
    await hass.async_block_till_done()
    assert calls["force_charge"]
    hass.states.async_set("sensor.power_sync_battery_mode", "force_charge", {"remaining_minutes": 29})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.energy_planner_battery_while_planner_charges").state == "30.0"
    hass.states.async_set("sensor.power_sync_battery_mode", "normal")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.energy_planner_battery_while_planner_charges").state == "unavailable"


async def test_battery_export_after_a_sale_is_not_spare_solar(hass, freezer):
    from custom_components.energy_planner.planner import Plan, Step

    entry, _ = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    switch_on = async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "number", "set_value")
    hass.states.async_set("sensor.charger_status_connector", "Preparing")
    # 17:00: the sale has just ended but the battery is still exporting 5 kW; no sun.
    hass.states.async_set("sensor.power_sync_grid_power", "-5.0", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.power_sync_solar_power", "0.0", {"unit_of_measurement": "kW"})
    freezer.tick(timedelta(minutes=10))
    step = Step(dt_util.now(), 0.5, "normal", 0, 0, 0.0, 0, 0.0, 1.0, 0.19, 0.08, 0, 70, 75, ev_solar=True)
    coordinator.data["plan"] = Plan([step], 0, None, None, None)
    coordinator.settings["mode"] = "Auto"
    await coordinator._follow_solar()
    assert not switch_on
    # Real spare solar still starts it.
    hass.states.async_set("sensor.power_sync_grid_power", "-3.0", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.power_sync_solar_power", "4.0", {"unit_of_measurement": "kW"})
    await coordinator._follow_solar()
    assert switch_on


def _forced_diag(hass, ems=2):
    hass.states.async_set(
        "sensor.power_sync_sungrow_diagnostics",
        "forced" if ems == 2 else "self_consumption",
        {"ems_mode": ems, "ems_mode_name": "forced" if ems == 2 else "self_consumption", "charge_cmd": 204, "min_soc": 5},
    )


async def test_resends_restore_when_the_inverter_stays_forced(hass, freezer):
    from custom_components.energy_planner.planner import Plan, Step

    entry, calls = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    step = Step(dt_util.now(), 0.5, "normal", 0, 0, 0.0, 0, 0.0, 1.0, 0.20, 0.10, -1.0, 50, None)
    coordinator.data["plan"] = Plan([step], 0, None, None, None)
    coordinator.settings["mode"] = "Auto"
    # PowerSync shows normal; its restore left the inverter forced and stopped.
    _forced_diag(hass)
    await coordinator._tick()
    assert not calls["restore_normal"]  # give PowerSync's own restore time to land
    freezer.tick(timedelta(seconds=160))
    await coordinator._tick()
    await hass.async_block_till_done()
    assert len(calls["restore_normal"]) == 1
    freezer.tick(timedelta(seconds=30))
    await coordinator._tick()
    assert len(calls["restore_normal"]) == 1  # still waiting on that one
    for _ in range(10):
        freezer.tick(timedelta(seconds=130))
        await coordinator._tick()
    assert len(calls["restore_normal"]) == 3  # then only the regular updates retry
    assert coordinator.stuck_restores == 3
    await coordinator._check_plan_followed(step, dt_util.utcnow(), 50.0, True)
    assert "inverter_forced" in coordinator._watchdog.incidents
    _forced_diag(hass, ems=0)
    await coordinator._tick()
    assert coordinator.stuck_restores == 0


async def test_alerts_when_the_battery_sits_idle_in_normal(hass):
    from custom_components.energy_planner.planner import Plan, Step

    entry, calls = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    step = Step(dt_util.now(), 0.5, "normal", 0, 0, 0.0, 0, 0.0, 1.0, 0.20, 0.10, -1.0, 50, None)
    plan = Plan([step], 0, None, None, None)
    coordinator.settings["mode"] = "Auto"
    hass.states.async_set("sensor.power_sync_battery_level", "50", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.power_sync_solar_power", "0.0", {"unit_of_measurement": "kW"})
    _forced_diag(hass, ems=0)
    # The battery covers the house: no problem.
    hass.states.async_set("sensor.power_sync_grid_power", "0.0", {"unit_of_measurement": "kW"})
    await coordinator._drive_battery(plan, dt_util.utcnow(), True)
    await coordinator._check_plan_followed(step, dt_util.utcnow(), 50.0, True)
    assert "battery_idle" not in coordinator._watchdog.incidents
    # The grid runs the house while the battery sits at 50%.
    hass.states.async_set("sensor.power_sync_grid_power", "0.98", {"unit_of_measurement": "kW"})
    await coordinator._drive_battery(plan, dt_util.utcnow(), True)
    await coordinator._check_plan_followed(step, dt_util.utcnow(), 50.0, True)
    assert "battery_idle" in coordinator._watchdog.incidents
    # Near the inverter's own floor that's expected.
    await coordinator._check_plan_followed(step, dt_util.utcnow(), 6.0, True)
    assert "battery_idle" not in coordinator._watchdog.incidents


async def test_takes_over_from_an_expired_powersync_hold(hass):
    _, calls = await _setup(hass)
    expired = (dt_util.utcnow() - timedelta(minutes=10)).isoformat()
    hass.states.async_set(
        "sensor.power_sync_battery_mode", "hold_soc", {"remaining_minutes": 0, "expires_at": expired, "locked_soc": 4.2}
    )
    hass.states.async_set("switch.power_sync_charge_by_time", "on")
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.energy_planner_mode", "option": "Auto"}, blocking=True
    )
    await hass.async_block_till_done()
    assert calls["force_charge"]
    status = hass.states.get("sensor.energy_planner_status")
    assert not status.state.startswith("Manual")
    assert any("Charge by time" in p for p in status.attributes["problems"])


async def test_a_running_powersync_hold_still_pauses_auto(hass):
    _, calls = await _setup(hass)
    later = (dt_util.utcnow() + timedelta(minutes=20)).isoformat()
    hass.states.async_set("sensor.power_sync_battery_mode", "hold_soc", {"remaining_minutes": 20, "expires_at": later})
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.energy_planner_mode", "option": "Auto"}, blocking=True
    )
    await hass.async_block_till_done()
    assert not calls["force_charge"]
    assert hass.states.get("sensor.energy_planner_status").state.startswith("Manual")


async def test_charge_by_time_is_flagged_while_its_switch_is_unavailable(hass):
    entry, _ = await _setup(hass)
    hass.states.async_set("switch.power_sync_charge_by_time", "unavailable")
    hass.states.async_set("sensor.power_sync_optimization_status", "disabled", {"charge_by_time_enabled": True})
    await hass.data[DOMAIN][entry.entry_id].async_refresh()
    await hass.async_block_till_done()
    problems = hass.states.get("sensor.energy_planner_status").attributes["problems"]
    assert any("Charge by time" in p for p in problems)


async def test_selling_floor_settings(hass):
    entry, _ = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert hass.states.get("number.energy_planner_selling_floor").state == "0.0"
    assert hass.states.get("time.energy_planner_selling_floor_from").state == "15:00:00"
    assert hass.states.get("time.energy_planner_selling_floor_until").state == "07:00:00"
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.energy_planner_selling_floor", "value": 30}, blocking=True
    )
    tz = dt_util.get_default_time_zone()
    evening = dt_util.now().astimezone(tz).replace(hour=20, minute=0)
    morning = evening.replace(hour=9)
    assert coordinator._sell_floor_now(evening) == 30.0
    assert coordinator._sell_floor_now(morning) == 0.0
