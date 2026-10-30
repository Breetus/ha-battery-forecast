"""Turn the first step of a plan into PowerSync and charger commands.

Pure logic with no Home Assistant imports. The integration feeds in what it
reads from Home Assistant and carries out the commands this returns.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .planner import MODE_CHARGE, MODE_DISCHARGE, MODE_HOLD, MODE_NORMAL, Step

# PowerSync battery_mode sensor states
PS_NORMAL = "normal"
PS_FORCE_CHARGE = "force_charge"
PS_FORCE_DISCHARGE = "force_discharge"
PS_HOLD = "hold_soc"
PS_SELF_CONSUMPTION = "self_consumption"

PS_MODE_FOR = {
    MODE_NORMAL: PS_NORMAL,
    MODE_HOLD: PS_HOLD,
    MODE_CHARGE: PS_FORCE_CHARGE,
    MODE_DISCHARGE: PS_FORCE_DISCHARGE,
}

COMMAND_MINUTES = 30
REFRESH_BELOW_MINUTES = 10
# How long PowerSync gets to show a command in its battery_mode sensor before
# a mismatch counts as a failed command rather than "still applying".
CONFIRM_SECONDS = 120
# The inverter's own floor is lower than the planner's reserve, so at the
# reserve a "normal" step becomes a hold. Released this far above the reserve.
RESERVE_RELEASE_PCT = 2.0

# Ignore power changes smaller than this, so the inverter isn't re-commanded
# for every small change in the plan.
POWER_DEADBAND_KW = 1.0


@dataclass
class Command:
    service: str  # power_sync service name
    data: dict = field(default_factory=dict)


@dataclass
class BatteryState:
    ps_mode: str | None  # sensor.power_sync_battery_mode
    remaining_minutes: float | None
    house_hold_active: bool  # the EV grid-only rule is holding the battery
    ev_charging: bool  # the car is drawing power right now
    soc_pct: float | None = None
    reserve_pct: float | None = None


@dataclass
class LastCommand:
    mode: str  # planner mode
    power_kw: float
    issued_at: datetime
    attempts: int = 1


@dataclass
class BatteryDecision:
    commands: list[Command]
    status: str  # what the controller is doing, for the dashboard
    override: bool = False  # a manual PowerSync command is in charge
    mode: str = ""  # the planner mode the commands put the battery in
    power_kw: float = 0.0


def manual_override(state: BatteryState, last: LastCommand | None, now: datetime) -> bool:
    """True when PowerSync is in a forced state that this controller did not ask for."""
    mode = state.ps_mode
    if mode in (None, PS_NORMAL, "unknown", "unavailable"):
        return False
    if mode == PS_HOLD and state.house_hold_active:
        return False  # the house EV rule, which the plan already allows for
    if last is None:
        return True
    if PS_MODE_FOR.get(last.mode) == mode:
        return False
    # PowerSync may still be applying our last command.
    return now - last.issued_at > timedelta(seconds=CONFIRM_SECONDS)


def decide_battery(step: Step, state: BatteryState, last: LastCommand | None, now: datetime) -> BatteryDecision:
    """Return the PowerSync commands that move the battery to the planned mode."""
    if manual_override(state, last, now):
        return BatteryDecision([], f"Manual {state.ps_mode} in control", override=True)

    mode = step.mode
    power = step.power_kw

    if mode == MODE_NORMAL and (state.ev_charging or state.house_hold_active):
        # The house rule holds the battery while the car charges; leave it be.
        return BatteryDecision([], "Normal (battery held while the car charges)")

    at_reserve = False
    if mode in (MODE_NORMAL, MODE_DISCHARGE) and state.soc_pct is not None and state.reserve_pct is not None:
        held = last is not None and last.mode == MODE_HOLD and state.ps_mode == PS_HOLD
        margin = RESERVE_RELEASE_PCT if held else 0.5
        if mode == MODE_DISCHARGE:
            margin = 0.0  # a sale runs right down to the reserve
        if state.soc_pct <= state.reserve_pct + margin:
            mode, power, at_reserve = MODE_HOLD, 0.0, True

    target = PS_MODE_FOR[mode]
    current = state.ps_mode
    pending = (
        last is not None
        and last.mode == mode
        and abs(last.power_kw - power) < POWER_DEADBAND_KW
        and now - last.issued_at < timedelta(seconds=CONFIRM_SECONDS)
    )

    if mode == MODE_NORMAL:
        if current in (PS_NORMAL, None) or pending:
            return BatteryDecision([], "Normal")
        return BatteryDecision([Command("restore_normal")], "Returning to normal", mode=MODE_NORMAL)

    label = {
        MODE_HOLD: "Holding at the reserve" if at_reserve else "Holding charge",
        MODE_CHARGE: f"Charging from grid at {power:g} kW",
        MODE_DISCHARGE: f"Selling to grid at {power:g} kW",
    }[mode]
    same_power = last is not None and last.mode == mode and abs(last.power_kw - power) < POWER_DEADBAND_KW
    running = current == target and (mode == MODE_HOLD or same_power)
    remaining = state.remaining_minutes
    if running and (remaining is None or remaining > REFRESH_BELOW_MINUTES):
        return BatteryDecision([], label)
    if pending:
        return BatteryDecision([], label)

    if mode == MODE_HOLD:
        cmd = Command("hold_battery_soc", {"duration": COMMAND_MINUTES})
    elif mode == MODE_CHARGE:
        cmd = Command("force_charge", {"duration": str(COMMAND_MINUTES), "power_w": int(power * 1000)})
    else:
        cmd = Command("force_discharge", {"duration": str(COMMAND_MINUTES), "power_w": int(power * 1000)})
    return BatteryDecision([cmd], label, mode=mode, power_kw=power)


@dataclass
class EvState:
    plugged_in: bool
    switch_on: bool | None
    current_a: float | None


@dataclass
class EvDecision:
    switch_on: bool | None = None  # None leaves the switch alone
    current_a: int | None = None  # None leaves the current alone
    status: str = ""


def decide_ev(step: Step, state: EvState, voltage: float = 230.0, min_a: int = 6, max_a: int = 32) -> EvDecision:
    """Return the charger changes that match the planned EV power."""
    if not state.plugged_in:
        return EvDecision(status="Car not plugged in")
    if step.ev_kw <= 0.05:
        return EvDecision(switch_on=False if state.switch_on is not False else None, status="Car waiting")
    amps = int(round(step.ev_kw * 1000 / voltage))
    amps = max(min_a, min(max_a, amps))
    change_a = amps if state.current_a is None or abs(state.current_a - amps) >= 1 else None
    switch = True if state.switch_on is not True else None
    return EvDecision(switch_on=switch, current_a=change_a, status=f"Charging car at {amps} A")
