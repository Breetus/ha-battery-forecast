"""Set-up form: which entities to read and drive, and the hardware limits."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_MODE,
    CONF_BATTERY_SOC,
    CONF_CHARGER_CURRENT,
    CONF_CHARGER_MAX_A,
    CONF_CHARGER_STATUS,
    CONF_CHARGER_SWITCH,
    CONF_CHARGER_VOLTAGE,
    CONF_EV_LIMIT,
    CONF_EV_POWER,
    CONF_EV_SOC,
    CONF_EXPORT_LIMIT,
    CONF_FEED_IN_ENTITY,
    CONF_HOUSE_HOLD,
    CONF_LOAD,
    CONF_LOAD_INCLUDES_EV,
    CONF_MAX_POWER,
    CONF_NOTIFY,
    CONF_PRICE_ENTITY,
    CONF_SOLAR_ENTITIES,
    DEFAULTS,
    DOMAIN,
)

_ENTITY = selector.EntitySelector()
_TEXT = selector.TextSelector()


def _num(lo, hi, step, unit):
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=lo, max=hi, step=step, unit_of_measurement=unit, mode="box")
    )


def _schema(values: dict) -> vol.Schema:
    v = {**DEFAULTS, **values}

    def req(key, sel):
        return (vol.Required(key, default=v[key]), sel)

    def opt(key, sel):
        value = v.get(key)
        marker = vol.Optional(key, description={"suggested_value": value}) if value else vol.Optional(key)
        return (marker, sel)

    fields = [
        req(CONF_BATTERY_SOC, _ENTITY),
        req(CONF_BATTERY_MODE, _ENTITY),
        req(CONF_LOAD, _ENTITY),
        req(CONF_LOAD_INCLUDES_EV, selector.BooleanSelector()),
        opt(CONF_EV_POWER, _ENTITY),
        opt(CONF_EV_SOC, _ENTITY),
        opt(CONF_EV_LIMIT, _ENTITY),
        opt(CONF_CHARGER_SWITCH, _ENTITY),
        opt(CONF_CHARGER_CURRENT, _ENTITY),
        opt(CONF_CHARGER_STATUS, _ENTITY),
        opt(CONF_HOUSE_HOLD, _ENTITY),
        opt(CONF_PRICE_ENTITY, _ENTITY),
        opt(CONF_FEED_IN_ENTITY, _ENTITY),
        opt(CONF_SOLAR_ENTITIES, _TEXT),
        opt(CONF_NOTIFY, _TEXT),
        req(CONF_BATTERY_CAPACITY, _num(1, 200, 0.1, "kWh")),
        req(CONF_MAX_POWER, _num(1, 50, 0.5, "kW")),
        req(CONF_EXPORT_LIMIT, _num(0, 50, 0.5, "kW")),
        req(CONF_CHARGER_VOLTAGE, _num(100, 400, 1, "V")),
        req(CONF_CHARGER_MAX_A, _num(6, 80, 1, "A")),
    ]
    return vol.Schema(dict(fields))


class EnergyPlannerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Energy Planner", data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(entry):
        return EnergyPlannerOptionsFlow()


class EnergyPlannerOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            # Fields left empty are dropped by the form; record them as cleared.
            cleared = {k: "" for k in (CONF_NOTIFY, CONF_EV_SOC, CONF_EV_LIMIT, CONF_PRICE_ENTITY, CONF_FEED_IN_ENTITY, CONF_SOLAR_ENTITIES) if k not in user_input}
            return self.async_create_entry(data={**cleared, **user_input})
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(current))
