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
    sell_floor_pct: float = 0.0  # a sale stops here; normal use carries on below it
    # PowerSync shows normal or a hold, but the inverter has stayed in forced
    # mode (a restore that didn't land, or PowerSync's own Charge by time), so
    # the battery isn't serving the house or taking the spare solar.
    inverter_forced: bool = False
    # The inverter's EMS is in forced mode right now, whatever PowerSync shows.
    ems_forced: bool = False
    # PowerSync still shows a forced mode or hold whose time ran out a while ago
    # (it sometimes never switches its mode back to normal).
    expired: bool = False


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
    target: str = ""  # the mode the battery should be in now (after the house rules)


def manual_override(state: BatteryState, last: LastCommand | None, now: datetime) -> bool:
    """True when PowerSync is in a forced state that this controller did not ask for."""
    mode = state.ps_mode
    if mode in (None, PS_NORMAL, "unknown", "unavailable"):
        return False
    if state.expired:
        return False  # a command that has run out isn't anyone's anymore
    if mode == PS_HOLD and state.house_hold_active:
        return False  # the house EV rule, which the plan already allows for
    if last is None:
        return True
    if PS_MODE_FOR.get(last.mode) == mode:
        return False
    # PowerSync may still be applying our last command.
    return now - last.issued_at > timedelta(seconds=CONFIRM_SECONDS)


def _just_sent(last: LastCommand | None, now: datetime) -> bool:
    """True while PowerSync may still be applying the controller's last command."""
    return last is not None and now - last.issued_at < timedelta(seconds=CONFIRM_SECONDS)


def decide_battery(step: Step, state: BatteryState, last: LastCommand | None, now: datetime) -> BatteryDecision:
    """Return the PowerSync commands that move the battery to the planned mode."""
    if manual_override(state, last, now):
        return BatteryDecision([], f"Manual {state.ps_mode} in control", override=True)

    mode = step.mode
    power = step.power_kw

    car_hold = False
    if mode == MODE_NORMAL and state.house_hold_active:
        # The house EV rule is holding the battery while the car charges.
        if state.inverter_forced and not _just_sent(last, now):
            # A real hold only caps discharge and leaves self-consumption on, so
            # the battery still takes the spare solar. Forced and stopped blocks
            # that, so put the inverter back and hold again straight away.
            commands = [Command("restore_normal")]
            if state.ps_mode == PS_HOLD:
                commands.append(Command("hold_battery_soc", {"duration": COMMAND_MINUTES}))
            return BatteryDecision(
                commands,
                "Holding while the car charges (the inverter was stuck in forced mode)",
                mode=MODE_HOLD if state.ps_mode == PS_HOLD else MODE_NORMAL,
                target=MODE_HOLD,
            )
        return BatteryDecision([], "Normal (battery held while the car charges)", target=MODE_HOLD)
    if mode == MODE_NORMAL and state.ev_charging:
        # The battery never feeds the car. The house rule only starts its hold
        # from normal, so after one of our commands we hold it ourselves.
        mode, car_hold = MODE_HOLD, True

    floor_reached = False
    if mode == MODE_DISCHARGE and state.soc_pct is not None and state.sell_floor_pct > 0:
        if state.soc_pct <= state.sell_floor_pct:
            mode, power, floor_reached = MODE_NORMAL, 0.0, True

    at_reserve = False
    # Only needed for a reserve above empty; at 0% the inverter stops by itself.
    if mode in (MODE_NORMAL, MODE_DISCHARGE) and state.soc_pct is not None and state.reserve_pct:
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
        if pending:
            return BatteryDecision([], "Normal", target=MODE_NORMAL)
        if current == PS_NORMAL and state.inverter_forced:
            return BatteryDecision(
                [Command("restore_normal")],
                "Returning to normal (the inverter was stuck in forced mode)",
                mode=MODE_NORMAL,
                target=MODE_NORMAL,
            )
        if current in (PS_NORMAL, None):
            return BatteryDecision([], "Normal (selling floor reached)" if floor_reached else "Normal", target=MODE_NORMAL)
        status = "Selling floor reached, returning to normal" if floor_reached else "Returning to normal"
        return BatteryDecision([Command("restore_normal")], status, mode=MODE_NORMAL, target=MODE_NORMAL)

    label = {
        MODE_HOLD: "Holding at the reserve"
        if at_reserve
        else ("Holding while the car charges" if car_hold else "Holding charge"),
        MODE_CHARGE: f"Charging from grid at {power:g} kW",
        MODE_DISCHARGE: f"Selling to grid at {power:g} kW",
    }[mode]
    if mode == MODE_HOLD and current == PS_HOLD and state.inverter_forced and not pending:
        return BatteryDecision(
            [Command("restore_normal"), Command("hold_battery_soc", {"duration": COMMAND_MINUTES})],
            f"{label} (the inverter was stuck in forced mode)",
            mode=MODE_HOLD,
            target=MODE_HOLD,
        )
    same_power = last is not None and last.mode == mode and abs(last.power_kw - power) < POWER_DEADBAND_KW
    running = current == target and (mode == MODE_HOLD or same_power)
    remaining = state.remaining_minutes
    if running and (remaining is None or remaining > REFRESH_BELOW_MINUTES):
        return BatteryDecision([], label, target=mode, power_kw=power)
    if pending:
        return BatteryDecision([], label, target=mode, power_kw=power)

    if mode == MODE_HOLD:
        cmd = Command("hold_battery_soc", {"duration": COMMAND_MINUTES})
    elif mode == MODE_CHARGE:
        cmd = Command("force_charge", {"duration": str(COMMAND_MINUTES), "power_w": int(power * 1000)})
    else:
        cmd = Command("force_discharge", {"duration": str(COMMAND_MINUTES), "power_w": int(power * 1000)})
    commands = [cmd]
    if current in (PS_NORMAL, None) and state.ems_forced:
        # PowerSync remembers the inverter's mode at the start of a force and
        # goes back to it afterwards. Starting from a leftover forced mode (for
        # example after a restart mid-sale), it would "restore" the inverter to
        # forced and leave the battery idle, so put it back to normal first.
        # A hold only caps discharge, so it needs the same: from forced mode
        # it would leave the battery unable to take the spare solar.
        commands.insert(0, Command("restore_normal"))
    return BatteryDecision(commands, label, mode=mode, power_kw=power, target=mode)


# Following spare solar: keep this much headroom so the car doesn't pull the
# last few hundred watts from the grid, stop only after the spare has stayed
# below the charger's minimum this long, and don't restart sooner than this
# after a stop, so the charger isn't cycled by passing clouds.
SPARE_MARGIN_KW = 0.2
SPARE_STOP_AFTER_S = 180
SPARE_RESTART_AFTER_S = 300
# Only ride out a dip while the panels still make at least this much above
# the house's own use; once the sun has gone the car stops straight away.
RIDE_OUT_MIN_SUN_KW = 0.5


@dataclass
class EvState:
    plugged_in: bool
    switch_on: bool | None
    current_a: float | None
    # What the car could draw without importing: its own draw plus the export
    # (or minus the import). None when the grid power isn't known.
    spare_kw: float | None = None
    sun_kw: float | None = None  # solar above the house's own use, None if unknown
    spare_short_for_s: float = 0.0  # how long the spare has been below the minimum
    off_for_s: float | None = None  # how long since the charger was switched off


@dataclass
class EvDecision:
    switch_on: bool | None = None  # None leaves the switch alone
    current_a: int | None = None  # None leaves the current alone
    status: str = ""


def decide_ev(step: Step, state: EvState, voltage: float = 230.0, min_a: int = 6, max_a: int = 32) -> EvDecision:
    """Return the charger changes that match the planned EV power.

    A step either follows the spare solar or sets a fixed level; at a fixed
    level the car also takes any spare solar above it.
    """
    if not state.plugged_in:
        return EvDecision(status="Car not plugged in")
    level_kw = step.ev_level_kw or (0.0 if step.ev_solar else step.ev_kw)
    if level_kw <= 0.05 and not step.ev_solar:
        return EvDecision(switch_on=False if state.switch_on is not False else None, status="Car waiting")

    base_a = max(min_a, min(max_a, int(round(level_kw * 1000 / voltage)))) if level_kw > 0.05 else 0
    if state.spare_kw is None:
        # Grid power unknown: fall back to the plan's own figure.
        spare_a = int(step.ev_kw * 1000 // voltage) if step.ev_solar else 0
    else:
        spare_a = int(max(state.spare_kw - SPARE_MARGIN_KW, 0.0) * 1000 // voltage)
    amps = min(max_a, max(base_a, spare_a))

    if amps < min_a:
        # Spare solar only, and there isn't enough of it for the charger's minimum.
        sunny = state.sun_kw is None or state.sun_kw >= RIDE_OUT_MIN_SUN_KW
        if state.switch_on and sunny and state.spare_short_for_s < SPARE_STOP_AFTER_S:
            amps = min_a  # ride out a passing cloud
        else:
            off = False if state.switch_on is not False else None
            return EvDecision(switch_on=off, status="Car waiting for spare solar")
    elif state.switch_on is not True and base_a == 0:
        if state.off_for_s is not None and state.off_for_s < SPARE_RESTART_AFTER_S:
            return EvDecision(status="Car waiting for spare solar")

    change_a = amps if state.current_a is None or abs(state.current_a - amps) >= 1 else None
    switch = True if state.switch_on is not True else None
    if spare_a >= amps:
        source = "spare solar"
    else:
        source = "spare solar and grid" if spare_a > 0 else "grid"
    return EvDecision(switch_on=switch, current_a=change_a, status=f"Charging car at {amps} A from {source}")
