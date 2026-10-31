"""What the planner is doing and what it expects."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import CONF_BATTERY_MODE, CONF_BATTERY_SOC, DOMAIN
from .entity import PlannerEntity
from .planner import MODE_CHARGE, MODE_DISCHARGE, MODE_HOLD, Plan, Step


def describe(step: Step) -> str:
    if step.mode == MODE_CHARGE:
        text = f"Charge battery {step.power_kw:g} kW"
    elif step.mode == MODE_DISCHARGE:
        text = f"Sell {step.power_kw:g} kW"
    elif step.mode == MODE_HOLD:
        text = "Hold battery"
    else:
        text = "Normal"
    if step.curtail:
        text += ", solar off"
    if step.ev_solar:
        text += ", car on spare solar"
    elif step.ev_kw > 0.05:
        text += f", car {step.ev_level_kw or step.ev_kw:.1f} kW"
    return text


def next_change(result: Plan) -> tuple[str, Step] | None:
    if not result or not result.steps:
        return None
    now = describe(result.steps[0])
    for step in result.steps[1:]:
        label = describe(step)
        if label != now:
            return label, step
    return None


async def async_setup_entry(hass, entry, async_add_entities):
    c = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            StatusSensor(c),
            PlanSensor(c),
            NextChangeSensor(c),
            PeakSocSensor(c, "battery_at_peak", "Battery at peak", "battery_soc_at_deadline"),
            PeakSocSensor(c, "ev_at_peak", "EV at peak", "ev_soc_at_deadline"),
            CostSensor(c),
            ChargingSocSensor(c),
        ]
    )


class StatusSensor(PlannerEntity, SensorEntity):
    _attr_icon = "mdi:state-machine"

    def __init__(self, c):
        super().__init__(c, "status", "Status")

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("battery_status", "Starting")[:255]

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data or {}
        return {
            "ev": d.get("ev_status"),
            "manual_override": d.get("override"),
            "problems": d.get("problems"),
            "price_source": d.get("price_source"),
            "ev_soc_used": self.coordinator.ev_soc(),
            "export_limit_kw": getattr(d.get("params"), "export_limit_kw", None),
            "inverter_stuck_restores": self.coordinator.stuck_restores,
            "updated": d.get("updated").isoformat() if d.get("updated") else None,
        }


class PlanSensor(PlannerEntity, SensorEntity):
    """State is the current step; the whole plan is in the 'steps' attribute."""

    _attr_icon = "mdi:chart-timeline"
    _unrecorded_attributes = frozenset({"steps", "deadline"})

    def __init__(self, c):
        super().__init__(c, "plan", "Plan now")

    @property
    def native_value(self):
        result = (self.coordinator.data or {}).get("plan")
        return describe(result.first) if result and result.first else None

    @property
    def extra_state_attributes(self):
        return self.coordinator.plan_attributes()


class NextChangeSensor(PlannerEntity, SensorEntity):
    _attr_icon = "mdi:clock-start"

    def __init__(self, c):
        super().__init__(c, "next_change", "Next change")

    @property
    def native_value(self):
        nxt = next_change((self.coordinator.data or {}).get("plan"))
        if not nxt:
            return "No change planned"
        label, step = nxt
        return f"{label} at {step.start.strftime('%H:%M')}"

    @property
    def extra_state_attributes(self):
        nxt = next_change((self.coordinator.data or {}).get("plan"))
        return {"at": nxt[1].start.isoformat()} if nxt else {}


class PeakSocSensor(PlannerEntity, SensorEntity):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery-clock"

    def __init__(self, c, key, name, attr):
        super().__init__(c, key, name)
        self._field = attr

    @property
    def native_value(self):
        result = (self.coordinator.data or {}).get("plan")
        return getattr(result, self._field, None) if result else None


class CostSensor(PlannerEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "AUD"
    _attr_icon = "mdi:cash"

    def __init__(self, c):
        super().__init__(c, "plan_cost", "Plan cost")

    @property
    def native_value(self):
        result = (self.coordinator.data or {}).get("plan")
        return result.total_cost if result else None

    @property
    def extra_state_attributes(self):
        result = (self.coordinator.data or {}).get("plan")
        if not result or not result.steps:
            return {}
        return {"horizon_end": (result.steps[-1].start).isoformat(), "note": "Expected net grid cost over the plan, after battery wear"}


class ChargingSocSensor(PlannerEntity, SensorEntity):
    """The battery SoC, but only while the planner is charging it from the grid.

    Unavailable the rest of the time, so a history chart draws just those
    stretches (the green line on the battery chart).
    """

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery-charging-high"

    def __init__(self, c):
        super().__init__(c, "charging_soc", "Battery while planner charges")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        watched = [e for e in (self.coordinator.conf(CONF_BATTERY_SOC), self.coordinator.conf(CONF_BATTERY_MODE)) if e]
        self.async_on_remove(async_track_state_change_event(self.hass, watched, self._changed))

    @callback
    def _changed(self, _event) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.planner_charging()

    @property
    def native_value(self):
        state = self.hass.states.get(self.coordinator.conf(CONF_BATTERY_SOC) or "")
        try:
            return float(state.state) if state else None
        except ValueError:
            return None
