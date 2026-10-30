"""Shared entity bits."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EnergyPlannerCoordinator


class PlannerEntity(CoordinatorEntity[EnergyPlannerCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: EnergyPlannerCoordinator, key: str, name: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_suggested_object_id = f"energy_planner_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name="Energy Planner",
            manufacturer="Local",
            model="Battery and EV optimiser",
        )
