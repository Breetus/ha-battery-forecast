"""Selectors: the planner mode, and which solar forecast to plan with."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, MODES, SETTING_MODE, SETTING_SOLAR_ESTIMATE, SOLAR_CAUTIOUS, SOLAR_ESTIMATES
from .entity import PlannerEntity

# Version 2 changed the solar forecast default from Cautious to Both, so a
# Cautious saved by version 1 (the old default) moves to the new one.
SETTINGS_VERSION = 2


async def async_setup_entry(hass, entry, async_add_entities):
    c = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            # Auto drives the battery and charger; Plan only just advises.
            SettingSelect(c, SETTING_MODE, "Mode", MODES, "mdi:robot"),
            SettingSelect(c, SETTING_SOLAR_ESTIMATE, "Solar forecast", SOLAR_ESTIMATES, "mdi:weather-partly-cloudy"),
        ]
    )


class SettingSelect(PlannerEntity, SelectEntity, RestoreEntity):
    def __init__(self, coordinator, key, name, options, icon):
        super().__init__(coordinator, key, name)
        self._attr_options = options
        self._attr_icon = icon

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in self._attr_options:
            old_default = (
                self._key == SETTING_SOLAR_ESTIMATE
                and last.state == SOLAR_CAUTIOUS
                and (last.attributes.get("settings_version") or 1) < SETTINGS_VERSION
            )
            if not old_default:
                self.coordinator.set_setting(self._key, last.state)

    @property
    def extra_state_attributes(self) -> dict:
        return {"settings_version": SETTINGS_VERSION}

    @property
    def current_option(self) -> str:
        return self.coordinator.settings[self._key]

    async def async_select_option(self, option: str) -> None:
        self.coordinator.set_setting(self._key, option)
        self.async_write_ha_state()
