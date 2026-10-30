"""On while the plan is being blocked (after the alert delay)."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .const import DOMAIN
from .entity import PlannerEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([PlanBlocked(hass.data[DOMAIN][entry.entry_id])])


class PlanBlocked(PlannerEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:alert-octagon-outline"

    def __init__(self, c):
        super().__init__(c, "plan_blocked", "Plan blocked")

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.blocked())

    @property
    def extra_state_attributes(self):
        return {"incidents": self.coordinator.incidents()}
