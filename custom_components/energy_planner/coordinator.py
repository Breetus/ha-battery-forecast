"""Gathers inputs, runs the planner and carries out its first step."""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_interval,
)
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
    CONF_EV_LIMIT,
    CONF_EV_POWER,
    CONF_EV_SOC,
    CONF_EXPORT_LIMIT,
    CONF_FEED_IN_ENTITY,
    CONF_GRID_POWER,
    CONF_HOUSE_HOLD,
    CONF_LOAD,
    CONF_LOAD_INCLUDES_EV,
    CONF_MAX_POWER,
    CONF_NOTIFY,
    CONF_PRICE_ENTITY,
    CONF_SOLAR_ENTITIES,
    CONF_SOLAR_POWER,
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
    SETTING_SOLAR_ESTIMATE,
    SETTING_WEAR,
    SOLAR_BOTH,
    SOLAR_CAUTIOUS,
    UPDATE_MINUTES,
)
from .control import PS_MODE_FOR, PS_NORMAL, BatteryState, EvState, LastCommand, decide_battery, decide_ev
from .planner import MODE_NORMAL, Params, Plan, Step, plan
from .watchdog import ResponseTracker, Watchdog

_LOGGER = logging.getLogger(__name__)

LOAD_HISTORY_DAYS = 10  # the recorder keeps 10 days by default
EV_EFFICIENCY = 0.9
EV_MIN_A = 6
# PowerSync's (locally patched) Sungrow sensor, which shows the inverter's own export limit.
SUNGROW_DIAGNOSTICS = "sensor.power_sync_sungrow_diagnostics"
# How often the charger is matched to the spare solar between plans.
EV_FOLLOW_SECONDS = 30
# How long the inverter may stay forced after PowerSync shows normal before
# the planner resends the restore. Covers the diagnostics sensor's polling lag.
INVERTER_STUCK_SECONDS = 150
# Between plans, resend a stuck restore at most this many times; after that
# only the regular plan updates retry it.
INVERTER_QUICK_RETRIES = 3
# A PowerSync command this long past its expiry time counts as over, even if
# PowerSync still shows it.
EXPIRED_GRACE_SECONDS = 120
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
        self._watchdog = Watchdog()
        self._response = ResponseTracker()
        self._battery_error: str | None = None
        self._boundary_unsub = None
        self._spare_short_since: datetime | None = None
        self._ev_off_at: datetime | None = None
        self._forced_since: datetime | None = None
        self._stuck_restores = 0

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
        prices = [
            e
            for e in (
                "sensor.power_sync_current_import_price",
                "sensor.power_sync_current_export_price",
                self.conf(CONF_PRICE_ENTITY),
                self.conf(CONF_FEED_IN_ENTITY),
            )
            if e
        ]
        # Re-plan as soon as the live price moves, so a spike is used while it lasts.
        self._unsubs.append(async_track_state_change_event(self.hass, prices, self._price_changed))
        soc_entity = self.conf(CONF_BATTERY_SOC)
        if soc_entity:
            self._unsubs.append(async_track_state_change_event(self.hass, [soc_entity], self._soc_changed))
        mode_entity = self.conf(CONF_BATTERY_MODE)
        if mode_entity:
            self._unsubs.append(async_track_state_change_event(self.hass, [mode_entity], self._battery_mode_changed))
        self._unsubs.append(
            async_track_time_interval(self.hass, self._tick, timedelta(seconds=EV_FOLLOW_SECONDS))
        )

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
        if self._ev_energy_at is not None and self._float_state(self.conf(CONF_EV_SOC)) is None:
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
    def _price_changed(self, event: Event) -> None:
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if old is not None and new is not None and old.state == new.state:
            return  # attribute-only change
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _soc_changed(self, event: Event) -> None:
        """Stop a sale promptly when the battery reaches the reserve."""
        soc = _num(getattr(event.data.get("new_state"), "state", None))
        last = self._last_battery
        if (
            soc is not None
            and last is not None
            and last.mode == "discharge"
            and float(self.settings[SETTING_RESERVE]) > 0
            and soc <= float(self.settings[SETTING_RESERVE])
        ):
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
        imports = self._price_entity(self.conf(CONF_PRICE_ENTITY), "general")
        exports = self._price_entity(self.conf(CONF_FEED_IN_ENTITY), "feedIn")
        if exports and self._feed_in_sign_flipped(exports):
            exports = [fc.Interval(i.start, i.end, -i.value) for i in exports]
        return imports, exports, "price entities"

    def _price_entity(self, entity: str | None, channel: str) -> list[fc.Interval]:
        state = self.hass.states.get(entity) if entity else None
        if state is None:
            return []
        items = state.attributes.get("forecasts") or state.attributes.get("forecast") or []
        items = [i for i in items if isinstance(i, dict)]
        if items and "time" in items[0] and "value" in items[0]:
            return fc.parse_series(items)  # Amber Express
        imp, exp = fc.parse_amber([{**i, "_channel": channel} for i in items])
        return exp if channel == "feedIn" else imp

    def _feed_in_sign_flipped(self, exports: list[fc.Interval]) -> bool:
        """True when the feed-in entity shows earnings as negative.

        Checked against PowerSync's current export price, which is positive
        when exporting earns money.
        """
        reference = self._float_state("sensor.power_sync_current_export_price")
        now = dt_util.utcnow()
        current = next((i.value for i in exports if i.start <= now < i.end), None)
        if reference is None or current is None or abs(reference) < 0.005 or abs(current) < 0.005:
            return False
        return (reference > 0) != (current > 0)

    def _solar(self) -> tuple[list[fc.Interval], list[fc.Interval] | None]:
        """The solar forecast to plan on and, when hedging, a typical day's."""
        setting = self.settings[SETTING_SOLAR_ESTIMATE]
        if setting == SOLAR_BOTH:
            low = self._solar_forecast("pv_estimate10")
            high = self._solar_forecast("pv_estimate")
            return low, (high or None)
        key = "pv_estimate10" if setting == SOLAR_CAUTIOUS else "pv_estimate"
        return self._solar_forecast(key), None

    def _solar_forecast(self, key: str) -> list[fc.Interval]:
        data = self._powersync_data("solcast_coordinator")
        if data and data.get("forecasts"):
            solar = fc.parse_solar(data["forecasts"], key=key)
            if solar:
                return solar
        solar: list[fc.Interval] = []
        for entity in [e.strip() for e in str(self.conf(CONF_SOLAR_ENTITIES) or "").split(",") if e.strip()]:
            state = self.hass.states.get(entity)
            if state is None:
                continue
            periods = state.attributes.get("detailedForecast") or state.attributes.get("forecast") or []
            solar += fc.parse_solar(periods, key=key)
        solar.sort(key=lambda i: i.start)
        return solar

    async def _load(self) -> list[float]:
        now = dt_util.utcnow()
        if self._load_profile is not None and self._load_profile_at and now - self._load_profile_at < timedelta(hours=6):
            return self._load_profile
        profile = [1.0] * 48
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import state_changes_during_period

            tz = dt_util.get_default_time_zone()
            start = now - timedelta(days=LOAD_HISTORY_DAYS)

            def _profile(entity_id: str) -> list[float] | None:
                history = state_changes_during_period(
                    self.hass, start, now, entity_id, no_attributes=True, include_start_time_state=True
                ).get(entity_id, [])
                unit_w = _is_watts(self.hass.states.get(entity_id))
                changes = []
                for st in history:
                    value = _num(st.state)
                    if value is not None and unit_w:
                        value /= 1000
                    changes.append((st.last_changed, value))
                if not any(v is not None for _, v in changes):
                    return None
                return fc.profile_from_changes(changes, now, tz)

            recorder = get_instance(self.hass)
            load = await recorder.async_add_executor_job(_profile, self.conf(CONF_LOAD))
            if load:
                if self.conf(CONF_LOAD_INCLUDES_EV) and self.conf(CONF_EV_POWER):
                    ev = await recorder.async_add_executor_job(_profile, self.conf(CONF_EV_POWER))
                    if ev:
                        load = [round(max(a - b, 0.2), 3) for a, b in zip(load, ev)]
                profile = load
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

    def _ev_level_now(self) -> float | None:
        """The level the last plan set for a car that is charging now (0 for spare solar)."""
        if self._ev_manual or _kw(self.hass.states.get(self.conf(CONF_EV_POWER) or "")) < 1.0:
            return None
        previous: Plan | None = (self.data or {}).get("plan")
        step = previous.first if previous else None
        if step is None:
            return None
        return 0.0 if step.ev_solar else (step.ev_level_kw or step.ev_kw)

    def _battery_mode_now(self) -> str | None:
        """The planner's own force charge or sale, if PowerSync is running it now."""
        last = self._last_battery
        mode = self.hass.states.get(self.conf(CONF_BATTERY_MODE) or "")
        if last is None or mode is None or PS_MODE_FOR.get(last.mode) != mode.state:
            return None
        return last.mode

    def planner_charging(self) -> bool:
        """True while PowerSync runs a force charge that the planner asked for."""
        return self._battery_mode_now() == "charge"

    def _export_limit_kw(self) -> float:
        """The configured export limit, capped at the inverter's own when PowerSync reports it."""
        limit = float(self.conf(CONF_EXPORT_LIMIT))
        diag = self.hass.states.get(SUNGROW_DIAGNOSTICS)
        if diag is None:
            return limit
        watts = _num(diag.attributes.get("export_limit_w"))
        enabled = next(
            (diag.attributes[k] for k in ("export_limit_enabled", "export_limit_enable") if k in diag.attributes), True
        )
        # Near zero is PowerSync curtailing a negative feed-in, not the inverter's setting.
        if watts is not None and watts >= 1000 and enabled not in (False, "false", "off", 0):
            limit = min(limit, watts / 1000)
        return limit

    def _ems_forced(self) -> bool:
        """True when the Sungrow diagnostics show the inverter's EMS in forced mode."""
        diag = self.hass.states.get(SUNGROW_DIAGNOSTICS)
        if diag is None:
            return False
        ems = _num(diag.attributes.get("ems_mode"))
        if ems is not None:
            return ems == 2
        return str(diag.attributes.get("ems_mode_name") or diag.state or "").lower() == "forced"

    def _inverter_forced(self, now: datetime) -> bool:
        """True when PowerSync has shown normal for a while but the inverter is still forced.

        PowerSync goes back to whatever mode the inverter was in when a force
        started. If that was itself forced (a restart mid-sale loses its record),
        its "restore" leaves the inverter forced and stopped while it says normal.
        """
        mode = self.hass.states.get(self.conf(CONF_BATTERY_MODE) or "")
        forced = mode is not None and mode.state == PS_NORMAL and self._ems_forced()
        if not forced:
            self._forced_since = None
            self._stuck_restores = 0
            return False
        if self._forced_since is None:
            self._forced_since = now
        since = max(self._forced_since, mode.last_changed)
        return (now - since).total_seconds() >= INVERTER_STUCK_SECONDS

    def _battery_idle(self, soc: float | None) -> str | None:
        """A problem message when the battery should run the house but the grid does."""
        if soc is None:
            return None
        diag = self.hass.states.get(SUNGROW_DIAGNOSTICS)
        floor = _num(diag.attributes.get("min_soc")) if diag is not None else None
        if soc <= max(float(self.settings[SETTING_RESERVE]), floor or 0.0) + 3:
            return None
        grid = self._live_kw(CONF_GRID_POWER)
        sun = self._sun_kw()  # solar minus the house's own use
        if grid is None or sun is None:
            return None
        need = -sun
        if need > 0.5 and grid > 0.5 and grid >= 0.7 * need:
            return (
                f"The plan has the battery running the house, but the house is drawing {grid:.1f} kW "
                f"from the grid while the battery sits at {soc:.0f}%."
            )
        return None

    def _live_kw(self, key: str) -> float | None:
        state = self.hass.states.get(self.conf(key) or "")
        if state is None or _num(state.state) is None:
            return None
        return _kw(state)

    def _sun_kw(self) -> float | None:
        """Solar output above the house's own use (the car not counted)."""
        solar = self._live_kw(CONF_SOLAR_POWER)
        load = self._live_kw(CONF_LOAD)
        if solar is None or load is None:
            return None
        if self.conf(CONF_LOAD_INCLUDES_EV):
            load -= max(_kw(self.hass.states.get(self.conf(CONF_EV_POWER) or "")), 0.0)
        return solar - load

    def _spare_kw(self) -> float | None:
        """Spare solar the car could draw without importing.

        Its draw plus the export, but never more than the solar left after the
        house: straight after a battery sale the export is the battery's, not
        the sun's.
        """
        grid = self._live_kw(CONF_GRID_POWER)  # + import, - export
        if grid is None:
            return None
        spare = max(_kw(self.hass.states.get(self.conf(CONF_EV_POWER) or "")), 0.0) - grid
        sun = self._sun_kw()
        return spare if sun is None else min(spare, sun)

    def _ev_plugged(self) -> bool:
        state = self.hass.states.get(self.conf(CONF_CHARGER_STATUS) or "")
        return state is not None and state.state in PLUGGED_STATES

    def ev_soc(self) -> float:
        soc = self._float_state(self.conf(CONF_EV_SOC))
        return soc if soc is not None else float(self.settings[SETTING_EV_SOC])

    def ev_target(self) -> float:
        """Our EV target, capped at the charge limit set in the car."""
        target = float(self.settings[SETTING_EV_TARGET])
        limit = self._float_state(self.conf(CONF_EV_LIMIT))
        return min(target, limit) if limit and limit > 0 else target

    # ----- planning ------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        tz = dt_util.get_default_time_zone()
        self._integrate_ev(_kw(self.hass.states.get(self.conf(CONF_EV_POWER) or "")))
        imports, exports, price_source = self._prices()
        solar, solar_high = self._solar()
        load = await self._load()
        slots = fc.build_slots(now, imports, exports, solar, load, tz, solar_high=solar_high)
        if slots:
            # At a negative feed-in PowerSync throttles the panels, so a low
            # reading then says nothing about the sun.
            fc.apply_live_solar(slots, self._live_kw(CONF_SOLAR_POWER), only_up=slots[0].export_price < 0)

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
            export_limit_kw=self._export_limit_kw(),
            import_limit_kw=float(self.settings[SETTING_IMPORT_LIMIT]),
            allow_grid_charge=bool(self.settings[SETTING_ALLOW_GRID_CHARGE]),
            allow_export=bool(self.settings[SETTING_ALLOW_SELL]),
            ev_connected=self._ev_plugged() and control_ev and not self._ev_manual,
            ev_capacity_kwh=float(self.settings[SETTING_EV_CAPACITY]),
            ev_soc_pct=self.ev_soc(),
            ev_efficiency=EV_EFFICIENCY,
            ev_target_pct=self.ev_target(),
            ev_levels_kw=tuple(round(a * voltage / 1000, 2) for a in sorted({EV_MIN_A, 16, max_a})),
            ev_min_kw=round(EV_MIN_A * voltage / 1000, 2),
            deadlines=fc.deadlines(now, tz, hh, mm),
            ev_level_now_kw=self._ev_level_now(),
            battery_mode_now=self._battery_mode_now(),
        )

        problems = []
        if soc is None:
            problems.append("battery SoC unavailable")
        if not slots:
            problems.append("no price forecast")
        if not solar:
            problems.append("no solar forecast (assuming none)")
        problems += self._conflicts(control_ev)

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
        if auto and result and result.first:
            await self._check_plan_followed(result.first, now, soc, control_ev)
        else:
            self._watchdog.clear()
            self._response.reset()

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

    def _conflicts(self, control_ev: bool) -> list[str]:
        """PowerSync features that would fight the planner."""
        out = []
        if switch_on(self.hass, "switch.power_sync_optimization_enabled"):
            out.append("PowerSync's own optimiser is on and will fight this one")
        status = self.hass.states.get("sensor.power_sync_optimization_status")
        by_time = status is not None and status.attributes.get("charge_by_time_enabled") in (True, "true", "on")
        if switch_on(self.hass, "switch.power_sync_charge_by_time") or by_time:
            out.append("PowerSync's Charge by time is on and charges the battery by itself")
        if control_ev:
            for state in self.hass.states.async_all("switch"):
                if state.entity_id.startswith("switch.power_sync_") and "solar_surplus" in state.entity_id and state.state == "on":
                    out.append(f"{state.entity_id} is on and may switch the charger too")
        return out

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
            soc_pct=self._float_state(self.conf(CONF_BATTERY_SOC)),
            reserve_pct=float(self.settings[SETTING_RESERVE]),
            inverter_forced=self._inverter_forced(now),
            ems_forced=self._ems_forced(),
            expired=_expired(mode_state, now),
        )
        decision = decide_battery(step, state, self._last_battery, now)
        self._battery_decision, self._battery_state = decision, state
        self._battery_error = None
        if not auto:
            return f"Plan only: {decision.status}", decision.override
        for cmd in decision.commands:
            try:
                await self.hass.services.async_call(
                    POWERSYNC_DOMAIN, cmd.service, cmd.data, blocking=True, context=self._context()
                )
            except Exception as err:  # noqa: BLE001 - retried on the next update
                _LOGGER.warning("PowerSync %s failed: %s", cmd.service, err)
                self._battery_error = f"PowerSync refused {cmd.service}: {err}"
                return f"{decision.status} (PowerSync refused: {err})", decision.override
            if cmd.service == "restore_normal" and state.inverter_forced:
                self._stuck_restores += 1
                _LOGGER.warning("Inverter still forced while PowerSync shows normal; resent restore_normal")
            elif cmd.service == "restore_normal" and decision.mode != MODE_NORMAL:
                _LOGGER.info("Inverter was left in forced mode; restored normal before the new command")
            _LOGGER.info("Sent PowerSync %s %s", cmd.service, cmd.data)
        if decision.commands:
            same = self._last_battery and self._last_battery.mode == decision.mode
            self._battery_attempts = self._battery_attempts + 1 if same else 1
            self._last_battery = LastCommand(decision.mode, decision.power_kw, now, self._battery_attempts)
            await self._save()
        status = decision.status
        if self._battery_attempts >= 3 and decision.commands:
            status += f" (retry {self._battery_attempts})"
        return status, decision.override

    async def _drive_ev(self, result: Plan, voltage: float, max_a: int, auto: bool, control_ev: bool) -> str:
        if not control_ev:
            return "EV control off"
        if self._ev_manual:
            return "Charger under manual control until the car is unplugged"
        now = dt_util.utcnow()
        switch = self.hass.states.get(self.conf(CONF_CHARGER_SWITCH) or "")
        switch_on = None if switch is None else switch.state == "on"
        spare = self._spare_kw()
        if spare is not None and switch_on and spare - 0.2 < EV_MIN_A * voltage / 1000:
            self._spare_short_since = self._spare_short_since or now
        else:
            self._spare_short_since = None
        if switch_on is False and self._ev_off_at is None and switch is not None:
            self._ev_off_at = switch.last_changed
        state = EvState(
            plugged_in=self._ev_plugged(),
            switch_on=switch_on,
            current_a=self._float_state(self.conf(CONF_CHARGER_CURRENT)),
            spare_kw=spare,
            sun_kw=self._sun_kw(),
            spare_short_for_s=(now - self._spare_short_since).total_seconds() if self._spare_short_since else 0.0,
            off_for_s=(now - self._ev_off_at).total_seconds() if switch_on is False and self._ev_off_at else None,
        )
        decision = decide_ev(result.first, state, voltage, min_a=EV_MIN_A, max_a=max_a)
        self._ev_state, self._ev_decision = state, decision
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
                self._ev_off_at = None if decision.switch_on else now
                self._spare_short_since = None
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Charger command failed: %s", err)
            return f"{decision.status} (charger refused: {err})"
        return decision.status

    async def _tick(self, _now=None) -> None:
        await self._watch_inverter()
        await self._follow_solar()

    async def _watch_inverter(self) -> None:
        """Between plans, put a stuck inverter back to normal."""
        result: Plan | None = (self.data or {}).get("plan")
        step = result.first if result else None
        now = dt_util.utcnow()
        if (
            not self._inverter_forced(now)
            or step is None
            or step.mode != MODE_NORMAL
            or self.settings[SETTING_MODE] != MODE_AUTO
            or self._stuck_restores >= INVERTER_QUICK_RETRIES
        ):
            return
        status, _ = await self._drive_battery(result, now, True)
        if self.data is not None and self.data.get("battery_status") != status:
            self.data["battery_status"] = status
            self.async_update_listeners()

    async def _follow_solar(self, _now=None) -> None:
        """Between plans, keep the charger matched to the spare solar."""
        result: Plan | None = (self.data or {}).get("plan")
        step = result.first if result else None
        if (
            step is None
            or not (step.ev_solar or step.ev_level_kw > 0)
            or self.settings[SETTING_MODE] != MODE_AUTO
            or not self.settings[SETTING_CONTROL_EV]
            or self._spare_kw() is None
        ):
            return
        status = await self._drive_ev(
            result, float(self.conf(CONF_CHARGER_VOLTAGE)), int(self.conf(CONF_CHARGER_MAX_A)), True, True
        )
        if self.data is not None and self.data.get("ev_status") != status:
            self.data["ev_status"] = status
            self.async_update_listeners()

    # ----- is the plan being carried out? ---------------------------------

    async def _check_plan_followed(self, step: Step, now: datetime, soc: float | None, control_ev: bool) -> None:
        """Alert the phone when the plan can't be carried out for a while."""
        from .sensor import describe

        problems: dict[str, str] = {}
        wanted = describe(step)
        decision = getattr(self, "_battery_decision", None)
        state = getattr(self, "_battery_state", None)
        if decision is not None and state is not None:
            if decision.override:
                who = self._powersync_source(state.ps_mode)
                problems["override"] = (
                    f"The plan is '{wanted}', but PowerSync is in {state.ps_mode}{who}, "
                    "which the planner didn't ask for, so it is standing aside."
                )
            elif self._battery_error:
                problems["refused"] = f"The plan is '{wanted}', but {self._battery_error}."
            elif state.inverter_forced:
                problems["inverter_forced"] = (
                    "PowerSync shows normal, but the inverter is still in forced mode, so the battery isn't "
                    f"covering the house. The planner has resent PowerSync's restore {self._stuck_restores} times."
                )
            elif decision.commands and self._battery_attempts >= 3:
                problems["refused"] = (
                    f"PowerSync hasn't taken the planner's command ({decision.status.lower()}) after "
                    f"{self._battery_attempts} tries; it still shows {state.ps_mode}."
                )
            target_ps = PS_MODE_FOR.get(decision.target)
            if decision.target in ("charge", "discharge", "hold") and state.ps_mode == target_ps:
                issue = self._response.problem(
                    decision.target, decision.power_kw, now, soc, float(self.conf(CONF_BATTERY_CAPACITY))
                )
                if issue:
                    problems["response"] = issue
            else:
                self._response.reset()
            if (
                decision.target == MODE_NORMAL
                and not decision.override
                and not state.inverter_forced
                and not state.house_hold_active
                and not state.ev_charging
            ):
                idle = self._battery_idle(soc)
                if idle:
                    problems["battery_idle"] = idle

        ev = getattr(self, "_ev_state", None)
        ev_decision = getattr(self, "_ev_decision", None)
        wants_car = step.ev_kw > 0.05 and not step.ev_solar
        if step.ev_solar and ev_decision is not None and ev is not None:
            # Spare solar only: the car is only expected to charge while the
            # charger has been switched on for it.
            wants_car = ev.switch_on is True and ev_decision.switch_on is not False
        if control_ev and wants_car and self._ev_plugged():
            if self._ev_manual:
                problems["ev_manual"] = (
                    "The plan wants to charge the car, but the charger was changed by hand, "
                    "so the planner is leaving it alone until the car is unplugged."
                )
            elif ev is not None and ev.switch_on is False:
                problems["ev_switch"] = "The plan wants to charge the car, but the charger's switch stays off."
            elif self._ev_power_kw < 1.0:
                problems["ev_power"] = (
                    "The charger is on for the car, but the car isn't drawing power. "
                    "Check the car's own charging timer or charge limit."
                )

        alerts, resolved = self._watchdog.update(now, problems)
        for message in alerts:
            await self._notify(message)
        for message in resolved:
            await self._notify(f"Sorted now: {message}")
        if alerts or resolved:
            self.async_update_listeners()

    def _powersync_source(self, ps_mode: str | None) -> str:
        """' (started from the app/dashboard)' etc., when PowerSync records who started it."""
        key = {"force_charge": "force_charge_state", "force_discharge": "force_discharge_state", "hold_soc": "hold_soc_state"}.get(
            ps_mode or ""
        )
        if not key:
            return ""
        for entry in self.hass.config_entries.async_entries(POWERSYNC_DOMAIN):
            runtime = self.hass.data.get(POWERSYNC_DOMAIN, {}).get(entry.entry_id)
            if isinstance(runtime, dict) and isinstance(runtime.get(key), dict):
                source = runtime[key].get("source")
                if source in ("user", "manual"):
                    return " (started by you)"
                if source:
                    return f" (started by {source})"
        return ""

    async def _notify(self, message: str) -> None:
        target = str(self.conf(CONF_NOTIFY) or "")
        if "." not in target:
            return
        domain, service = target.split(".", 1)
        try:
            await self.hass.services.async_call(
                domain, service, {"title": "Energy Planner", "message": message}, blocking=False, context=self._context()
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not send the alert through %s: %s", target, err)

    @property
    def stuck_restores(self) -> int:
        """Restores resent because the inverter stayed forced while PowerSync showed normal."""
        return self._stuck_restores

    def blocked(self) -> list[str]:
        return self._watchdog.blocked()

    def incidents(self) -> list[dict]:
        return [
            {"problem": i.message, "since": i.since.isoformat(), "alerted": i.alerted}
            for i in self._watchdog.incidents.values()
        ]

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


def _expired(state, now: datetime) -> bool:
    """True when PowerSync's mode sensor shows a command whose expiry time passed a while ago."""
    if state is None or state.state in (PS_NORMAL, STATE_UNKNOWN, STATE_UNAVAILABLE):
        return False
    attrs = state.attributes
    raw = attrs.get("expires_at") or attrs.get("force_expires_at")
    expires = dt_util.parse_datetime(raw) if isinstance(raw, str) else raw if isinstance(raw, datetime) else None
    if expires is None:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=dt_util.UTC)
    return (now - expires).total_seconds() > EXPIRED_GRACE_SECONDS


def switch_on(hass: HomeAssistant, entity_id: str) -> bool:
    state = hass.states.get(entity_id)
    return state is not None and state.state == "on"


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
