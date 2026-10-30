"""Constants for Energy Planner."""
from __future__ import annotations

DOMAIN = "energy_planner"
POWERSYNC_DOMAIN = "power_sync"

PLATFORMS = ["select", "number", "switch", "time", "sensor"]

UPDATE_MINUTES = 5

# Config entry keys (entities the planner reads or drives)
CONF_BATTERY_SOC = "battery_soc_entity"
CONF_BATTERY_MODE = "battery_mode_entity"
CONF_LOAD = "load_entity"
CONF_LOAD_INCLUDES_EV = "load_includes_ev"
CONF_EV_POWER = "ev_power_entity"
CONF_EV_SOC = "ev_soc_entity"
CONF_EV_LIMIT = "ev_limit_entity"
CONF_CHARGER_SWITCH = "charger_switch_entity"
CONF_CHARGER_CURRENT = "charger_current_entity"
CONF_CHARGER_STATUS = "charger_status_entity"
CONF_HOUSE_HOLD = "house_hold_entity"
CONF_PRICE_ENTITY = "price_forecast_entity"
CONF_FEED_IN_ENTITY = "feed_in_forecast_entity"
CONF_SOLAR_ENTITIES = "solar_forecast_entities"
CONF_BATTERY_CAPACITY = "battery_capacity_kwh"
CONF_MAX_POWER = "battery_max_power_kw"
CONF_EXPORT_LIMIT = "export_limit_kw"
CONF_CHARGER_VOLTAGE = "charger_voltage"
CONF_CHARGER_MAX_A = "charger_max_current"

DEFAULTS = {
    CONF_BATTERY_SOC: "sensor.power_sync_battery_level",
    CONF_BATTERY_MODE: "sensor.power_sync_battery_mode",
    CONF_LOAD: "sensor.power_sync_home_load",
    CONF_LOAD_INCLUDES_EV: False,
    CONF_EV_POWER: "sensor.ev_charging_power",
    CONF_EV_SOC: "sensor.wmw52ga0507r62550_battery_hv_state_of_charge",
    CONF_EV_LIMIT: "sensor.wmw52ga0507r62550_battery_ev_target_state_of_charge",
    CONF_CHARGER_SWITCH: "switch.charger_charge_control",
    CONF_CHARGER_CURRENT: "number.charger_maximum_current",
    CONF_CHARGER_STATUS: "sensor.charger_status_connector",
    CONF_HOUSE_HOLD: "input_boolean.ev_grid_only_hold_active",
    CONF_PRICE_ENTITY: "sensor.amber_express_home_general_price",
    CONF_FEED_IN_ENTITY: "sensor.amber_express_home_feed_in_price",
    CONF_SOLAR_ENTITIES: "sensor.solcast_pv_forecast_forecast_today, sensor.solcast_pv_forecast_forecast_tomorrow, sensor.solcast_pv_forecast_forecast_day_3",
    CONF_BATTERY_CAPACITY: 20.0,
    CONF_MAX_POWER: 10.0,
    CONF_EXPORT_LIMIT: 10.0,
    CONF_CHARGER_VOLTAGE: 240.0,
    CONF_CHARGER_MAX_A: 32,
}

# Runtime settings, each backed by a number/switch/select/time entity.
MODE_AUTO = "Auto"
MODE_PLAN_ONLY = "Plan only"
MODES = [MODE_AUTO, MODE_PLAN_ONLY]

SOLAR_EXPECTED = "Expected (50%)"
SOLAR_CAUTIOUS = "Cautious (10%)"
SOLAR_ESTIMATES = [SOLAR_EXPECTED, SOLAR_CAUTIOUS]

SETTING_MODE = "mode"
SETTING_SOLAR_ESTIMATE = "solar_estimate"
SETTING_BATTERY_TARGET = "battery_target"
SETTING_EV_TARGET = "ev_target"
SETTING_RESERVE = "battery_reserve"
SETTING_WEAR = "wear_cost"
SETTING_EV_SOC = "ev_soc"
SETTING_EV_CAPACITY = "ev_capacity"
SETTING_IMPORT_LIMIT = "import_limit"
SETTING_PEAK_START = "peak_start"
SETTING_ALLOW_SELL = "allow_selling"
SETTING_ALLOW_GRID_CHARGE = "allow_grid_charging"
SETTING_CONTROL_EV = "control_ev"

SETTING_DEFAULTS = {
    SETTING_MODE: MODE_PLAN_ONLY,
    # Plan on Solcast's 10th percentile, so a dull day doesn't leave the
    # battery short at the deadline after selling in the morning.
    SETTING_SOLAR_ESTIMATE: SOLAR_CAUTIOUS,
    SETTING_BATTERY_TARGET: 80.0,
    SETTING_EV_TARGET: 80.0,
    SETTING_RESERVE: 0.0,  # the battery normally runs right down
    SETTING_WEAR: 5.0,  # c/kWh
    SETTING_EV_SOC: 50.0,
    SETTING_EV_CAPACITY: 64.7,  # MINI Countryman SE ALL4 (U25) usable
    SETTING_IMPORT_LIMIT: 14.0,
    SETTING_PEAK_START: "15:00",  # Amber steps up from off-peak at 3 pm
    SETTING_ALLOW_SELL: True,
    SETTING_ALLOW_GRID_CHARGE: True,
    SETTING_CONTROL_EV: True,
}

# OCPP connector states that mean a car is plugged in
PLUGGED_STATES = {"Preparing", "Charging", "SuspendedEV", "SuspendedEVSE", "Finishing"}
