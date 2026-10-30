"""Mode selector: Auto drives the battery and charger, Plan only just advises."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, MODES, SETTING_MODE
from .entity import PlannerEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ModeSelect(hass.data[DOMAIN][entry.entry_id])])


class ModeSelect(PlannerEntity, SelectEntity, RestoreEntity):
    _attr_options = MODES
    _attr_icon = "mdi:robot"

    def __init__(self, coordinator):
        super().__init__(coordinator, SETTING_MODE, "Mode")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in MODES:
            self.coordinator.set_setting(SETTING_MODE, last.state)

    @property
    def current_option(self) -> str:
        return self.coordinator.settings[SETTING_MODE]

    async def async_select_option(self, option: str) -> None:
        self.coordinator.set_setting(SETTING_MODE, option)
        self.async_write_ha_state()
