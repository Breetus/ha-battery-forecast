"""Targets, limits and the EV state-of-charge estimate."""
from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower

from .const import (
    CONF_EV_SOC,
    DOMAIN,
    SETTING_BATTERY_TARGET,
    SETTING_EV_CAPACITY,
    SETTING_EV_SOC,
    SETTING_EV_TARGET,
    SETTING_IMPORT_LIMIT,
    SETTING_RESERVE,
    SETTING_WEAR,
)
from .entity import PlannerEntity

# key, name, min, max, step, unit, icon
NUMBERS = [
    (SETTING_BATTERY_TARGET, "Battery target at peak", 0, 100, 5, PERCENTAGE, "mdi:battery-arrow-up"),
    (SETTING_EV_TARGET, "EV target at peak", 0, 100, 5, PERCENTAGE, "mdi:car-electric"),
    (SETTING_RESERVE, "Battery reserve", 0, 50, 1, PERCENTAGE, "mdi:battery-lock"),
    (SETTING_WEAR, "Battery wear cost", 0, 30, 0.5, "c/kWh", "mdi:battery-heart-variant"),
    (SETTING_EV_CAPACITY, "EV battery size", 10, 120, 0.5, UnitOfEnergy.KILO_WATT_HOUR, "mdi:car-battery"),
    (SETTING_IMPORT_LIMIT, "Grid import limit", 3, 30, 0.5, UnitOfPower.KILO_WATT, "mdi:transmission-tower"),
]


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [SettingNumber(coordinator, *n) for n in NUMBERS]
    if not coordinator.conf(CONF_EV_SOC):
        entities.append(
            SettingNumber(coordinator, SETTING_EV_SOC, "EV state of charge", 0, 100, 1, PERCENTAGE, "mdi:car-electric-outline")
        )
    async_add_entities(entities)


class SettingNumber(PlannerEntity, RestoreNumber):
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, key, name, lo, hi, step, unit, icon):
        super().__init__(coordinator, key, name)
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last and last.native_value is not None:
            self.coordinator.set_setting(self._key, float(last.native_value))

    @property
    def native_value(self) -> float:
        value = float(self.coordinator.settings[self._key])
        return round(value, 1) if self._key == SETTING_EV_SOC else value

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.set_setting(self._key, float(value))
        self.async_write_ha_state()
