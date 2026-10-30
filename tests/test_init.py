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


async def _setup(hass):
    now = dt_util.utcnow()
    ps_entry = MockConfigEntry(domain="power_sync", title="PowerSync")
    ps_entry.add_to_hass(hass)
    hass.data["power_sync"] = {
        ps_entry.entry_id: {
            "amber_coordinator": SimpleNamespace(data={"forecast": _amber_forecast(now), "current": []}),
            "solcast_coordinator": SimpleNamespace(data={"forecasts": []}),
        }
    }
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
        "number.energy_planner_ev_state_of_charge",
        "switch.energy_planner_allow_selling_to_grid",
        "time.energy_planner_peak_price_starts",
        "sensor.energy_planner_battery_at_peak",
        "sensor.energy_planner_next_change",
    ):
        assert hass.states.get(entity_id) is not None, entity_id


async def test_auto_charges_plugged_in_car(hass):
    _, calls = await _setup(hass)
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
