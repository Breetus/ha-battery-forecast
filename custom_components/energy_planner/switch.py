"""On/off preferences for the planner."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, SETTING_ALLOW_GRID_CHARGE, SETTING_ALLOW_SELL, SETTING_CONTROL_EV
from .entity import PlannerEntity

SWITCHES = [
    (SETTING_ALLOW_SELL, "Allow selling to grid", "mdi:transmission-tower-export"),
    (SETTING_ALLOW_GRID_CHARGE, "Allow grid charging", "mdi:transmission-tower-import"),
    (SETTING_CONTROL_EV, "Control EV charger", "mdi:ev-station"),
]


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(SettingSwitch(coordinator, *s) for s in SWITCHES)


class SettingSwitch(PlannerEntity, SwitchEntity, RestoreEntity):
    def __init__(self, coordinator, key, name, icon):
        super().__init__(coordinator, key, name)
        self._attr_icon = icon

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in ("on", "off"):
            self.coordinator.set_setting(self._key, last.state == "on")

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.settings[self._key])

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.set_setting(self._key, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.set_setting(self._key, False)
        self.async_write_ha_state()
