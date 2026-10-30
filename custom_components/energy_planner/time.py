"""When the evening peak starts (the deadline for the targets)."""
from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, SETTING_PEAK_START
from .entity import PlannerEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([PeakStart(hass.data[DOMAIN][entry.entry_id])])


class PeakStart(PlannerEntity, TimeEntity, RestoreEntity):
    _attr_icon = "mdi:clock-alert-outline"

    def __init__(self, coordinator):
        super().__init__(coordinator, SETTING_PEAK_START, "Peak price starts")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state.count(":") >= 1:
            try:
                hh, mm = (int(x) for x in last.state.split(":")[:2])
            except ValueError:
                return
            self.coordinator.set_setting(SETTING_PEAK_START, f"{hh:02d}:{mm:02d}")

    @property
    def native_value(self) -> time:
        hh, mm = (int(x) for x in self.coordinator.settings[SETTING_PEAK_START].split(":")[:2])
        return time(hh, mm)

    async def async_set_value(self, value: time) -> None:
        self.coordinator.set_setting(SETTING_PEAK_START, f"{value.hour:02d}:{value.minute:02d}")
        self.async_write_ha_state()
