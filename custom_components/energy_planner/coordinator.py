"""Gathers inputs, runs the planner and carries out its first step."""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import forecast as fc
from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_MODE,
    CONF_BATTERY_SOC,
    CONF_CHARGER_CURRENT,
    CONF_CHARGER_MAX_A,
    CONF_CHARGER_STATUS,
    CONF_CHARGER_SWITCH,
    CONF_CHARGER_VOLTAGE,
    CONF_EV_POWER,
    CONF_EV_SOC,
    CONF_EXPORT_LIMIT,
    CONF_FEED_IN_ENTITY,
    CONF_HOUSE_HOLD,
    CONF_LOAD,
    CONF_LOAD_INCLUDES_EV,
    CONF_MAX_POWER,
    CONF_PRICE_ENTITY,
    CONF_SOLAR_ENTITIES,
    DEFAULTS,
    DOMAIN,
    MODE_AUTO,
    PLUGGED_STATES,
    POWERSYNC_DOMAIN,
    SETTING_ALLOW_GRID_CHARGE,
    SETTING_ALLOW_SELL,
    SETTING_BATTERY_TARGET,
    SETTING_CONTROL_EV,
    SETTING_DEFAULTS,
    SETTING_EV_CAPACITY,
    SETTING_EV_SOC,
    SETTING_EV_TARGET,
    SETTING_IMPORT_LIMIT,
    SETTING_MODE,
    SETTING_PEAK_START,
    SETTING_RESERVE,
    SETTING_WEAR,
    UPDATE_MINUTES,
)
from .control import BatteryState, EvState, LastCommand, decide_battery, decide_ev
from .planner import Params, Plan, plan

_LOGGER = logging.getLogger(__name__)

LOAD_HISTORY_DAYS = 14
EV_EFFICIENCY = 0.9
STORE_VERSION = 1


class EnergyPlannerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Plans every few minutes and, in Auto, drives PowerSync and the charger."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(minutes=UPDATE_MINUTES),
        )
        self.entry = entry
        self.settings: dict[str, Any] = dict(SETTING_DEFAULTS)
        self._store = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._last_battery: LastCommand | None = None
        self._battery_attempts = 0
        self._load_profile: list[float] | None = None
        self._load_profile_at: datetime | None = None
        self._our_contexts: list[str] = []
        self._ev_manual = False
        self._ev_energy_at: datetime | None = None
        self._ev_power_kw = 0.0
        self._unsubs: list = []
        self._boundary_unsub = None

    # ----- configuration -------------------------------------------------

    def conf(self, key: str):
        merged = {**DEFAULTS, **self.entry.data, **self.entry.options}
        return merged.get(key, DEFAULTS.get(key))

    async def async_setup(self) -> None:
        stored = await self._store.async_load() or {}
        last = stored.get("last_battery")
        if last:
            try:
                self._last_battery = LastCommand(
                    mode=last["mode"],
                    power_kw=float(last["power_kw"]),
                    issued_at=dt_util.parse_datetime(last["issued_at"]),
                )
            except (KeyError, TypeError, ValueError):
                self._last_battery = None
        ev_power = self.conf(CONF_EV_POWER)
        if ev_power:
            self._unsubs.append(async_track_state_change_event(self.hass, [ev_power], self._ev_power_changed))
        switch = self.conf(CONF_CHARGER_SWITCH)
        status = self.conf(CONF_CHARGER_STATUS)
        watched = [e for e in (switch, self.conf(CONF_CHARGER_CURRENT)) if e]
        if watched:
            self._unsubs.append(async_track_state_change_event(self.hass, watched, self._charger_changed))
        if status:
            self._unsubs.append(async_track_state_change_event(self.hass, [status], self._connector_changed))
        mode_entity = self.conf(CONF_BATTERY_MODE)
        if mode_entity:
            self._unsubs.append(async_track_state_change_event(self.hass, [mode_entity], self._battery_mode_changed))

    @callback
    def async_shutdown_listeners(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._boundary_unsub:
            self._boundary_unsub()
            self._boundary_unsub = None

    async def _save(self) -> None:
        last = self._last_battery
        await self._store.async_save(
            {
                "last_battery": None
                if last is None
                else {"mode": last.mode, "power_kw": last.power_kw, "issued_at": last.issued_at.isoformat()}
            }
        )

    def set_setting(self, key: str, value) -> None:
        """Called by the setting entities; re-plans soon after a change."""
        if self.settings.get(key) == value:
            return
        self.settings[key] = value
        if self.data is not None:
            self.hass.async_create_task(self.async_request_refresh())

    # ----- listeners -----------------------------------------------------

    @callback
    def _ev_power_changed(self, event: Event) -> None:
        self._integrate_ev(_kw(event.data.get("new_state")))

    @callback
    def _integrate_ev(self, new_kw: float) -> None:
        """Add charger energy since the last reading to the EV SoC estimate."""
        now = dt_util.utcnow()
        if self._ev_energy_at is not None and not self.conf(CONF_EV_SOC):
            hours = (now - self._ev_energy_at).total_seconds() / 3600
            cap = float(self.settings[SETTING_EV_CAPACITY]) or 1.0
            gained = self._ev_power_kw * hours * EV_EFFICIENCY / cap * 100
            if gained > 0:
                soc = min(100.0, float(self.settings[SETTING_EV_SOC]) + gained)
                self.settings[SETTING_EV_SOC] = round(soc, 2)
                self.async_update_listeners()
        self._ev_energy_at = now
        self._ev_power_kw = new_kw

    @callback
    def _charger_changed(self, event: Event) -> None:
        """A person changing the charger from the UI takes over until unplug."""
        new = event.data.get("new_state")
        if new is None or new.context.id in self._our_contexts:
            return
        if new.context.user_id and self.settings.get(SETTING_MODE) == MODE_AUTO:
            if not self._ev_manual:
                _LOGGER.info("Charger changed by hand; leaving the car alone until it is unplugged")
            self._ev_manual = True
            self.async_update_listeners()

    @callback
    def _connector_changed(self, event: Event) -> None:
        new = event.data.get("new_state")
        if new is not None and new.state not in PLUGGED_STATES and self._ev_manual:
            self._ev_manual = False
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _battery_mode_changed(self, event: Event) -> None:
        self.hass.async_create_task(self.async_request_refresh())

    # ----- inputs --------------------------------------------------------

    def _powersync_data(self, key: str) -> dict | None:
        for entry in self.hass.config_entries.async_entries(POWERSYNC_DOMAIN):
            runtime = self.hass.data.get(POWERSYNC_DOMAIN, {}).get(entry.entry_id)
            if not isinstance(runtime, dict):
                continue
            coordinator = runtime.get(key)
            data = getattr(coordinator, "data", None)
            if isinstance(data, dict):
                return data
        return None

    def _prices(self) -> tuple[list[fc.Interval], list[fc.Interval], str]:
        data = self._powersync_data("amber_coordinator")
        if data:
            raw = list(data.get("forecast") or []) + list(data.get("current") or [])
            imports, exports = fc.parse_amber(raw)
            if imports:
                return imports, exports, "PowerSync Amber"
        imports: list[fc.Interval] = []
        exports: list[fc.Interval] = []
        for key, target in ((CONF_PRICE_ENTITY, "general"), (CONF_FEED_IN_ENTITY, "feedIn")):
            entity = self.conf(key)
            state = self.hass.states.get(entity) if entity else None
            if state is None:
                continue
            items = state.attributes.get("forecasts") or state.attributes.get("forecast") or []
            items = [{**i, "_channel": target} for i in items if isinstance(i, dict)]
            imp, exp = fc.parse_amber(items)
            imports += imp
            exports += exp
        return imports, exports, "price entities"

    def _solar(self) -> list[fc.Interval]:
        data = self._powersync_data("solcast_coordinator")
        if data and data.get("forecasts"):
            solar = fc.parse_solar(data["forecasts"])
            if solar:
                return solar
        solar: list[fc.Interval] = []
        for entity in [e.strip() for e in str(self.conf(CONF_SOLAR_ENTITIES) or "").split(",") if e.strip()]:
            state = self.hass.states.get(entity)
            if state is None:
                continue
            periods = state.attributes.get("detailedForecast") or state.attributes.get("forecast") or []
            solar += fc.parse_solar(periods)
        solar.sort(key=lambda i: i.start)
        return solar

    async def _load(self) -> list[float]:
        now = dt_util.utcnow()
        if self._load_profile is not None and self._load_profile_at and now - self._load_profile_at < timedelta(hours=6):
            return self._load_profile
        profile = [1.0] * 48
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import statistics_during_period

            load_id = self.conf(CONF_LOAD)
            ev_id = self.conf(CONF_EV_POWER) if self.conf(CONF_LOAD_INCLUDES_EV) else None
            ids = {i for i in (load_id, ev_id) if i}
            start = now - timedelta(days=LOAD_HISTORY_DAYS)
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period, self.hass, start, now, ids, "hour", None, {"mean"}
            )
            tz = dt_util.get_default_time_zone()
            ev_by_hour = {row["start"]: row.get("mean") or 0.0 for row in stats.get(ev_id, [])} if ev_id else {}
            samples = []
            load_unit_w = _is_watts(self.hass.states.get(load_id))
            ev_unit_w = _is_watts(self.hass.states.get(ev_id)) if ev_id else False
            for row in stats.get(load_id, []):
                mean = row.get("mean")
                if mean is None:
                    continue
                kw = mean / 1000 if load_unit_w else mean
                ev = ev_by_hour.get(row["start"], 0.0)
                kw -= ev / 1000 if ev_unit_w else ev
                when = dt_util.utc_from_timestamp(row["start"]) if isinstance(row["start"], (int, float)) else row["start"]
                for half in (0, 30):
                    samples.append((when + timedelta(minutes=half), max(kw, 0.2)))
            if samples:
                profile = fc.profile_from_samples(samples, tz)
        except Exception:  # noqa: BLE001 - the plan still works on a flat guess
            _LOGGER.exception("Could not read load history; assuming 1 kW")
        self._load_profile = profile
        self._load_profile_at = now
        return profile

    def _float_state(self, entity: str | None) -> float | None:
        state = self.hass.states.get(entity) if entity else None
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    def _ev_plugged(self) -> bool:
        state = self.hass.states.get(self.conf(CONF_CHARGER_STATUS) or "")
        return state is not None and state.state in PLUGGED_STATES

    def ev_soc(self) -> float:
        soc = self._float_state(self.conf(CONF_EV_SOC))
        return soc if soc is not None else float(self.settings[SETTING_EV_SOC])

    # ----- planning ------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        tz = dt_util.get_default_time_zone()
        self._integrate_ev(_kw(self.hass.states.get(self.conf(CONF_EV_POWER) or "")))
        imports, exports, price_source = self._prices()
        solar = self._solar()
        load = await self._load()
        slots = fc.build_slots(now, imports, exports, solar, load, tz)

        soc = self._float_state(self.conf(CONF_BATTERY_SOC))
        hh, mm = (int(x) for x in str(self.settings[SETTING_PEAK_START]).split(":")[:2])
        max_kw = float(self.conf(CONF_MAX_POWER))
        voltage = float(self.conf(CONF_CHARGER_VOLTAGE))
        max_a = int(self.conf(CONF_CHARGER_MAX_A))
        control_ev = bool(self.settings[SETTING_CONTROL_EV])
        params = Params(
            battery_capacity_kwh=float(self.conf(CONF_BATTERY_CAPACITY)),
            battery_soc_pct=soc if soc is not None else 50.0,
            battery_reserve_pct=float(self.settings[SETTING_RESERVE]),
            battery_target_pct=float(self.settings[SETTING_BATTERY_TARGET]),
            max_charge_kw=max_kw,
            max_discharge_kw=max_kw,
            power_levels_kw=tuple(round(max_kw * f, 2) for f in (0.25, 0.5, 0.75, 1.0)),
            wear_cost=float(self.settings[SETTING_WEAR]) / 100,
            export_limit_kw=float(self.conf(CONF_EXPORT_LIMIT)),
            import_limit_kw=float(self.settings[SETTING_IMPORT_LIMIT]),
            allow_grid_charge=bool(self.settings[SETTING_ALLOW_GRID_CHARGE]),
            allow_export=bool(self.settings[SETTING_ALLOW_SELL]),
            ev_connected=self._ev_plugged() and control_ev and not self._ev_manual,
            ev_capacity_kwh=float(self.settings[SETTING_EV_CAPACITY]),
            ev_soc_pct=self.ev_soc(),
            ev_efficiency=EV_EFFICIENCY,
            ev_target_pct=float(self.settings[SETTING_EV_TARGET]),
            ev_levels_kw=tuple(round(a * voltage / 1000, 2) for a in sorted({6, 16, max_a})),
            deadlines=fc.deadlines(now, tz, hh, mm),
        )

        problems = []
        if soc is None:
            problems.append("battery SoC unavailable")
        if not slots:
            problems.append("no price forecast")
        if not solar:
            problems.append("no solar forecast (assuming none)")

        result: Plan | None = None
        if slots and soc is not None:
            result = await self.hass.async_add_executor_job(plan, slots, params)

        battery_status = "Idle"
        ev_status = ""
        override = False
        auto = self.settings[SETTING_MODE] == MODE_AUTO
        if result and result.first:
            battery_status, override = await self._drive_battery(result, now, auto)
            ev_status = await self._drive_ev(result, voltage, max_a, auto, control_ev)
        elif problems:
            battery_status = "Not planning: " + ", ".join(problems)

        self._schedule_boundary(slots)
        return {
            "updated": now,
            "plan": result,
            "params": params,
            "battery_status": battery_status,
            "ev_status": ev_status,
            "override": override,
            "problems": problems,
            "price_source": price_source,
            "load_profile": load,
        }

    def _schedule_boundary(self, slots) -> None:
        """Re-plan just after the next slot starts, so commands change on time."""
        if self._boundary_unsub:
            self._boundary_unsub()
            self._boundary_unsub = None
        if len(slots) < 2:
            return
        when = dt_util.as_utc(slots[1].start) + timedelta(seconds=15)

        @callback
        def _fire(_now) -> None:
            self._boundary_unsub = None
            self.hass.async_create_task(self.async_request_refresh())

        self._boundary_unsub = async_track_point_in_utc_time(self.hass, _fire, when)

    # ----- actions -------------------------------------------------------

    def _context(self) -> Context:
        ctx = Context()
        self._our_contexts = (self._our_contexts + [ctx.id])[-50:]
        return ctx

    async def _drive_battery(self, result: Plan, now: datetime, auto: bool) -> tuple[str, bool]:
        step = result.first
        mode_state = self.hass.states.get(self.conf(CONF_BATTERY_MODE) or "")
        hold = self.hass.states.get(self.conf(CONF_HOUSE_HOLD) or "")
        state = BatteryState(
            ps_mode=mode_state.state if mode_state else None,
            remaining_minutes=_num(mode_state.attributes.get("remaining_minutes")) if mode_state else None,
            house_hold_active=hold is not None and hold.state == "on",
            ev_charging=self._ev_power_kw > 1.0 or _kw(self.hass.states.get(self.conf(CONF_EV_POWER) or "")) > 1.0,
        )
        decision = decide_battery(step, state, self._last_battery, now)
        if not auto:
            return f"Plan only: {decision.status}", decision.override
        for cmd in decision.commands:
            try:
                await self.hass.services.async_call(
                    POWERSYNC_DOMAIN, cmd.service, cmd.data, blocking=True, context=self._context()
                )
            except Exception as err:  # noqa: BLE001 - retried on the next update
                _LOGGER.warning("PowerSync %s failed: %s", cmd.service, err)
                return f"{decision.status} (PowerSync refused: {err})", decision.override
            same = self._last_battery and self._last_battery.mode == step.mode
            self._battery_attempts = self._battery_attempts + 1 if same else 1
            self._last_battery = LastCommand(step.mode, step.power_kw, now, self._battery_attempts)
            await self._save()
            _LOGGER.info("Sent PowerSync %s %s", cmd.service, cmd.data)
        status = decision.status
        if self._battery_attempts >= 3 and decision.commands:
            status += f" (retry {self._battery_attempts})"
        return status, decision.override

    async def _drive_ev(self, result: Plan, voltage: float, max_a: int, auto: bool, control_ev: bool) -> str:
        if not control_ev:
            return "EV control off"
        if self._ev_manual:
            return "Charger under manual control until the car is unplugged"
        switch = self.hass.states.get(self.conf(CONF_CHARGER_SWITCH) or "")
        current = self._float_state(self.conf(CONF_CHARGER_CURRENT))
        state = EvState(
            plugged_in=self._ev_plugged(),
            switch_on=None if switch is None else switch.state == "on",
            current_a=current,
        )
        decision = decide_ev(result.first, state, voltage, max_a=max_a)
        if not auto:
            return f"Plan only: {decision.status}"
        try:
            if decision.current_a is not None and self.conf(CONF_CHARGER_CURRENT):
                await self.hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": self.conf(CONF_CHARGER_CURRENT), "value": decision.current_a},
                    blocking=True,
                    context=self._context(),
                )
            if decision.switch_on is not None and switch is not None:
                await self.hass.services.async_call(
                    "switch",
                    "turn_on" if decision.switch_on else "turn_off",
                    {"entity_id": switch.entity_id},
                    blocking=True,
                    context=self._context(),
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Charger command failed: %s", err)
            return f"{decision.status} (charger refused: {err})"
        return decision.status

    def plan_attributes(self) -> dict[str, Any]:
        data = self.data or {}
        result: Plan | None = data.get("plan")
        if not result:
            return {}
        steps = []
        for s in result.steps:
            d = asdict(s)
            d["start"] = s.start.isoformat()
            steps.append(d)
        return {"steps": steps, "deadline": result.deadline.isoformat() if result.deadline else None}


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_watts(state) -> bool:
    return state is not None and state.attributes.get("unit_of_measurement") == "W"


def _kw(state) -> float:
    if state is None:
        return 0.0
    value = _num(state.state)
    if value is None:
        return 0.0
    return value / 1000 if _is_watts(state) else value
