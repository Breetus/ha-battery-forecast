"""Cost-minimising plan for the house battery and the EV.

Pure Python and numpy, with no Home Assistant imports, so it can be tested and
simulated on any machine.

The planner works on fixed slots (normally 30 minutes) and runs a backward
dynamic programme over a two-dimensional state: energy stored in the house
battery and energy delivered to the car. In every slot it chooses one battery
mode (normal self-consumption, hold, force charge at a power level or force
discharge at a power level) and one EV charge level, which is either "spare
solar only" or a fixed level that also takes any spare solar above it. Missing
the battery or EV target at a deadline (the start of the evening peak) costs a
large penalty per kWh short, so the cheapest plan that meets both targets wins.

When slots carry a second, sunnier solar forecast, the programme runs once per
forecast and each step is the one with the lowest average cost across the two.
That stops the plan buying grid energy now that the sun is likely to provide,
and selling energy now that a dull afternoon would need back, while the dull
day's deadline penalty still forces any purchase that can't wait.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

MODE_NORMAL = "normal"
MODE_HOLD = "hold"
MODE_CHARGE = "charge"
MODE_DISCHARGE = "discharge"

# A tiny cost per slot for every non-default action, so that equal-cost plans
# prefer leaving the inverter alone, the car on spare solar only and the
# panels on.
_COMMAND_NUDGE = {MODE_NORMAL: 0.0, MODE_HOLD: 0.0005, MODE_CHARGE: 0.001, MODE_DISCHARGE: 0.001}
_EV_NUDGE = 0.0005
_CURTAIL_NUDGE = 0.0005
# $/kWh charged for grid import above the site supply limit.
_OVERLOAD_PENALTY = 100.0
# Cost that rules an action out. A force discharge needs at least half of its
# power available above the reserve: below that the inverter would keep
# discharging toward its own (lower) minimum while the plan expects it to stop.
_INFEASIBLE = 1000.0
_MIN_DISCHARGE_SHARE = 0.5


@dataclass
class Slot:
    """One planning interval with its forecasts."""

    start: datetime
    hours: float
    import_price: float  # $/kWh paid for import
    export_price: float  # $/kWh earned for export (negative means you pay)
    solar_kw: float  # forecast PV output (on a dull day, when solar_high_kw is set)
    load_kw: float  # forecast house load, excluding the EV
    solar_high_kw: float | None = None  # PV output on a typical day, to hedge between the two


@dataclass
class Params:
    """Physical limits, targets and preferences."""

    battery_capacity_kwh: float = 20.0
    battery_soc_pct: float = 50.0
    battery_reserve_pct: float = 10.0
    battery_max_pct: float = 100.0
    battery_target_pct: float = 80.0
    max_charge_kw: float = 10.0
    max_discharge_kw: float = 10.0
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    wear_cost: float = 0.05  # $/kWh taken out of the battery
    power_levels_kw: tuple[float, ...] = (2.5, 5.0, 7.5, 10.0)
    export_limit_kw: float = 10.0
    import_limit_kw: float = 14.0
    allow_grid_charge: bool = True
    allow_export: bool = True
    # The panels can be switched off when the import price is negative, so the
    # house, battery and car all draw from the grid and get paid for it.
    can_curtail_solar: bool = False

    ev_connected: bool = False
    ev_capacity_kwh: float = 37.0
    ev_soc_pct: float = 0.0
    ev_target_pct: float = 80.0
    ev_levels_kw: tuple[float, ...] = (1.4, 3.7, 7.4)
    ev_min_kw: float = 1.4  # the charger's lowest current; spare solar below it is left alone
    ev_efficiency: float = 0.9

    deadlines: list[datetime] = field(default_factory=list)
    shortfall_penalty: float = 20.0  # $/kWh below target at a deadline
    terminal_value_per_kwh: float | None = None
    # Weight of the dull-day forecast when slots also carry a typical-day one.
    dull_day_weight: float = 0.5

    # What is running now, so the first step only stops or changes it when that
    # saves more than a few cents. Without this, near-equal prices flip the plan
    # at every re-plan and the car gets stopped a minute after each start.
    ev_level_now_kw: float | None = None  # fixed car level running now (0 for spare solar), None if the car isn't charging
    battery_mode_now: str | None = None
    ev_change_cost: float = 0.05  # $ to stop the car or change its level
    battery_change_cost: float = 0.02  # $ to end a running force charge or sale

    battery_step_kwh: float = 0.25
    ev_step_kwh: float = 0.5


@dataclass
class Step:
    """The chosen action for one slot and its expected outcome."""

    start: datetime
    hours: float
    mode: str
    power_kw: float  # requested force charge/discharge power, 0 otherwise
    battery_kw: float  # AC battery power, + charging / - discharging
    ev_kw: float  # AC power to the car
    grid_kw: float  # + import / - export
    solar_kw: float
    load_kw: float
    import_price: float
    export_price: float
    cost: float  # $ for this slot (negative is income)
    battery_soc_pct: float  # at the end of the slot
    ev_soc_pct: float  # at the end of the slot
    ev_solar: bool = False  # the car follows the spare solar
    ev_level_kw: float = 0.0  # the car's fixed level, which it tops up with spare solar
    curtail: bool = False  # panels off (the import price is negative)


@dataclass
class Plan:
    steps: list[Step]
    total_cost: float
    battery_soc_at_deadline: float | None
    ev_soc_at_deadline: float | None
    deadline: datetime | None

    @property
    def first(self) -> Step | None:
        return self.steps[0] if self.steps else None


@dataclass(frozen=True)
class _Action:
    mode: str
    power_kw: float
    ev_kw: float  # fixed car level; spare solar above it goes to the car too
    ev_solar: bool = False  # car on spare solar only
    curtail: bool = False  # panels off


def _actions(p: Params, ev_needed: bool, curtail_ok: bool = False) -> list[_Action]:
    # Spare-solar charging comes before "off", so a tie keeps the car following
    # the sun rather than leaving it parked while solar is exported.
    ev_options = [(0.0, False)]
    if ev_needed:
        ev_options = [(0.0, True), (0.0, False)] + [(lvl, False) for lvl in p.ev_levels_kw if lvl > 0]
    actions = []
    for curtail in (False, True) if curtail_ok else (False,):
        for ev, solar in ev_options:
            actions.append(_Action(MODE_NORMAL, 0.0, ev, solar, curtail))
            actions.append(_Action(MODE_HOLD, 0.0, ev, solar, curtail))
            if p.allow_grid_charge:
                for lvl in p.power_levels_kw:
                    if 0 < lvl <= p.max_charge_kw + 1e-9:
                        actions.append(_Action(MODE_CHARGE, lvl, ev, solar, curtail))
            # The battery never feeds the car, so no force discharge while it charges.
            if p.allow_export and ev == 0 and not solar:
                for lvl in p.power_levels_kw:
                    if 0 < lvl <= p.max_discharge_kw + 1e-9:
                        actions.append(_Action(MODE_DISCHARGE, lvl, 0.0, False, curtail))
    return actions


def _simulate(slot: Slot, b, e, act: _Action, p: Params, ev_need_kwh: float, solar_kw: float | None = None):
    """Apply one action to arrays of battery energy b and EV energy e.

    Returns (cost, next_b, next_e, battery_kw, ev_ac_kw, grid_kw); every result
    broadcasts over the shapes of b and e.
    """
    h = slot.hours
    eta_c, eta_d = p.charge_efficiency, p.discharge_efficiency
    b_min = p.battery_capacity_kwh * p.battery_reserve_pct / 100
    b_max = p.battery_capacity_kwh * p.battery_max_pct / 100
    solar = 0.0 if act.curtail else (slot.solar_kw if solar_kw is None else solar_kw)
    spare = max(solar - slot.load_kw, 0.0)  # solar left after the house

    room_ac = np.maximum(b_max - b, 0.0) / (eta_c * h)
    avail_ac = np.maximum(b - b_min, 0.0) * eta_d / h
    zero = np.zeros(np.broadcast(b, e).shape)

    ev_ac = zero
    if act.ev_kw > 0 or act.ev_solar:
        # The battery takes its share of the spare solar first (the charger
        # follows what would otherwise be exported), the car the rest on top
        # of its fixed level.
        if act.mode == MODE_CHARGE:
            battery_share = np.minimum(act.power_kw, room_ac)
        elif act.mode == MODE_DISCHARGE:
            battery_share = zero
        else:
            battery_share = np.minimum(np.minimum(max(spare - act.ev_kw, 0.0), p.max_charge_kw), room_ac)
        ev_max = max(max(p.ev_levels_kw, default=0.0), act.ev_kw)
        want = np.minimum(act.ev_kw + np.maximum(spare - act.ev_kw - battery_share, 0.0), ev_max)
        if act.ev_solar:
            want = np.where(want >= p.ev_min_kw - 1e-9, want, 0.0)
        ev_kwh = np.minimum(want * h * p.ev_efficiency, np.maximum(ev_need_kwh - e, 0.0))
        ev_ac = ev_kwh / p.ev_efficiency / h + zero
    net = slot.load_kw + ev_ac - solar  # demand left after solar
    surplus = np.maximum(-net, 0.0)
    deficit = np.maximum(net, 0.0)

    # While the car charges, the existing house rule holds the battery so it
    # never feeds the car; normal mode then behaves like hold.
    if act.mode in (MODE_NORMAL, MODE_HOLD):
        pc = np.minimum(np.minimum(surplus, p.max_charge_kw), room_ac)
        pd = zero
        if act.mode == MODE_NORMAL:
            pd = np.where(ev_ac > 1e-9, 0.0, np.minimum(np.minimum(deficit, p.max_discharge_kw), avail_ac))
    elif act.mode == MODE_CHARGE:
        pc = np.minimum(act.power_kw, room_ac)
        pd = zero
    else:
        pc = zero
        pd = np.minimum(act.power_kw, avail_ac)
    pc = pc + zero
    pd = pd + zero

    grid = net + pc - pd
    imp = np.maximum(grid, 0.0)
    exp = np.minimum(np.maximum(-grid, 0.0), p.export_limit_kw)
    if slot.export_price < 0 and act.mode != MODE_DISCHARGE:
        exp = zero  # PowerSync curtails solar export at a negative feed-in price
    grid = imp - exp

    cost = (
        imp * h * slot.import_price
        - exp * h * slot.export_price
        + pd * h * p.wear_cost
        + np.maximum(imp - p.import_limit_kw, 0.0) * h * _OVERLOAD_PENALTY
        + _COMMAND_NUDGE[act.mode]
        + (_EV_NUDGE if act.ev_kw else 0.0)
        + (_CURTAIL_NUDGE if act.curtail else 0.0)
    )
    if act.mode == MODE_DISCHARGE:
        cost = cost + np.where(avail_ac < act.power_kw * _MIN_DISCHARGE_SHARE, _INFEASIBLE, 0.0)
    next_b = b + pc * eta_c * h - pd * h / eta_d
    next_e = e + ev_kwh if (act.ev_kw > 0 or act.ev_solar) else e + zero
    return cost, next_b, next_e, pc - pd, ev_ac, grid


def _interp_2d(v, b_grid, e_grid, b, e):
    """Bilinear interpolation of v[b_grid, e_grid] at points (b, e)."""
    nb, ne = len(b_grid), len(e_grid)
    if nb > 1:
        fb = np.clip((b - b_grid[0]) / (b_grid[1] - b_grid[0]), 0, nb - 1)
    else:
        fb = np.zeros_like(b)
    if ne > 1:
        fe = np.clip((e - e_grid[0]) / (e_grid[1] - e_grid[0]), 0, ne - 1)
    else:
        fe = np.zeros_like(e)
    fb, fe = np.broadcast_arrays(fb, fe)
    i0 = np.minimum(np.floor(fb).astype(int), max(nb - 2, 0))
    j0 = np.minimum(np.floor(fe).astype(int), max(ne - 2, 0))
    i1 = np.minimum(i0 + 1, nb - 1)
    j1 = np.minimum(j0 + 1, ne - 1)
    wb = fb - i0
    we = fe - j0
    return (
        v[i0, j0] * (1 - wb) * (1 - we)
        + v[i1, j0] * wb * (1 - we)
        + v[i0, j1] * (1 - wb) * we
        + v[i1, j1] * wb * we
    )


def _grid(lo: float, hi: float, step: float) -> np.ndarray:
    if hi <= lo + 1e-9:
        return np.array([lo])
    n = max(int(np.ceil((hi - lo) / step)), 1)
    return np.linspace(lo, hi, n + 1)


def plan(slots: list[Slot], p: Params) -> Plan:
    """Return the cheapest plan over the given slots."""
    if not slots:
        return Plan([], 0.0, None, None, None)

    cap = p.battery_capacity_kwh
    b_min = cap * p.battery_reserve_pct / 100
    b_max = cap * p.battery_max_pct / 100
    b0 = min(max(cap * p.battery_soc_pct / 100, b_min), b_max)
    b_target = min(cap * p.battery_target_pct / 100, b_max)

    ev_need = 0.0
    if p.ev_connected:
        ev_need = max(p.ev_capacity_kwh * (p.ev_target_pct - p.ev_soc_pct) / 100, 0.0)
    ev_needed = ev_need > 0.05

    b_grid = _grid(b_min, b_max, p.battery_step_kwh)
    # A big top-up gets a coarser grid, so the plan stays quick on a Raspberry Pi.
    e_grid = _grid(0.0, ev_need, max(p.ev_step_kwh, ev_need / 40)) if ev_needed else np.array([0.0])
    B = b_grid[:, None]
    E = e_grid[None, :]
    plain = _actions(p, ev_needed)
    # Switching the panels off only pays while the grid pays you to import.
    with_curtail = _actions(p, ev_needed, curtail_ok=True) if p.can_curtail_solar else plain

    def actions_for(slot: Slot) -> list[_Action]:
        return with_curtail if slot.import_price < 0 else plain

    # Penalty for being short of target, added to the value at the start of the
    # first slot that begins at or after each deadline.
    penalty_at: dict[int, float] = {}
    first_deadline_idx = None
    for dl in sorted(p.deadlines):
        idx = next((i for i, s in enumerate(slots) if s.start >= dl), None)
        if idx is None or idx == 0:
            continue
        penalty_at[idx] = p.shortfall_penalty
        if first_deadline_idx is None:
            first_deadline_idx = idx

    def deadline_penalty(idx: int):
        weight = penalty_at.get(idx)
        if not weight:
            return 0.0
        short = np.maximum(b_target - B, 0.0) + np.maximum(ev_need - E, 0.0)
        return weight * short

    terminal = p.terminal_value_per_kwh
    if terminal is None:
        prices = sorted(s.import_price for s in slots)
        terminal = max(prices[len(prices) // 4] * p.discharge_efficiency - p.wear_cost, 0.0)

    n = len(slots)
    # The first forecast (a dull day when there are two) is the one the plan
    # shows; the second only steers the choice of each step.
    forecasts = [[s.solar_kw for s in slots]]
    if any(s.solar_high_kw is not None for s in slots):
        forecasts.append([s.solar_kw if s.solar_high_kw is None else s.solar_high_kw for s in slots])
    weights = [1.0] if len(forecasts) == 1 else [p.dull_day_weight, 1 - p.dull_day_weight]

    def backward(solar: list[float]) -> list:
        value = -(B - b_min) * terminal + np.zeros_like(E)
        values = [None] * (n + 1)
        values[n] = value + deadline_penalty(n)
        for t in range(n - 1, -1, -1):
            best = None
            for act in actions_for(slots[t]):
                cost, nb_, ne_, *_ = _simulate(slots[t], B, E, act, p, ev_need, solar[t])
                total = cost + _interp_2d(values[t + 1], b_grid, e_grid, nb_, ne_)
                best = total if best is None else np.minimum(best, total)
            values[t] = best + deadline_penalty(t)
        return values

    value_sets = [backward(solar) for solar in forecasts]

    # Forward pass from the real (continuous) state, along the first forecast.
    steps: list[Step] = []
    b, e = np.array(b0), np.array(0.0)
    total_cost = 0.0
    batt_at_dl = ev_at_dl = None
    for t, slot in enumerate(slots):
        if t == first_deadline_idx:
            batt_at_dl = float(b) / cap * 100
            ev_at_dl = _ev_pct(p, float(e)) if p.ev_connected else None
        best = None
        for act in actions_for(slot):
            score = _change_cost(p, act, ev_needed) if t == 0 else 0.0
            outcome = None
            for weight, solar, values in zip(weights, forecasts, value_sets):
                cost, nb_, ne_, bkw, evkw, gkw = _simulate(slot, b, e, act, p, ev_need, solar[t])
                score += weight * float(cost + _interp_2d(values[t + 1], b_grid, e_grid, nb_, ne_))
                if outcome is None:
                    outcome = (float(cost), float(nb_), float(ne_), float(bkw), float(evkw), float(gkw))
            if best is None or score < best[0] - 1e-9:
                best = (score, act, *outcome)
        _, act, cost, nb_, ne_, bkw, evkw, gkw = best
        b, e = np.array(nb_), np.array(ne_)
        real_cost = (
            cost
            - _COMMAND_NUDGE[act.mode]
            - (_EV_NUDGE if act.ev_kw else 0.0)
            - (_CURTAIL_NUDGE if act.curtail else 0.0)
        )
        total_cost += real_cost
        steps.append(
            Step(
                start=slot.start,
                hours=slot.hours,
                mode=act.mode,
                power_kw=act.power_kw,
                battery_kw=round(bkw, 3),
                ev_kw=round(evkw, 3),
                grid_kw=round(gkw, 3),
                solar_kw=0.0 if act.curtail else slot.solar_kw,
                load_kw=slot.load_kw,
                import_price=slot.import_price,
                export_price=slot.export_price,
                cost=round(real_cost, 4),
                battery_soc_pct=round(float(b) / cap * 100, 1),
                ev_soc_pct=round(_ev_pct(p, float(e)), 1) if p.ev_connected else None,
                ev_solar=act.ev_solar,
                ev_level_kw=act.ev_kw,
                curtail=act.curtail,
            )
        )
    if first_deadline_idx is None and p.deadlines and n:
        # Deadline at or past the end of the horizon: report the final state.
        if slots[-1].start < min(p.deadlines):
            batt_at_dl = float(b) / cap * 100
            ev_at_dl = _ev_pct(p, float(e)) if p.ev_connected else None
    deadline = slots[first_deadline_idx].start if first_deadline_idx is not None else None
    return Plan(
        steps,
        round(total_cost, 2),
        None if batt_at_dl is None else round(batt_at_dl, 1),
        None if ev_at_dl is None else round(ev_at_dl, 1),
        deadline,
    )


def _change_cost(p: Params, act: _Action, ev_needed: bool) -> float:
    """Cost of stopping or changing what is running now (first step only)."""
    cost = 0.0
    # Spare-solar charging counts as level 0, and stopping from it is free.
    if p.ev_level_now_kw is not None and ev_needed and abs(act.ev_kw - p.ev_level_now_kw) > 0.05:
        cost += p.ev_change_cost
    if p.battery_mode_now in (MODE_CHARGE, MODE_DISCHARGE) and act.mode != p.battery_mode_now:
        cost += p.battery_change_cost
    return cost


def _ev_pct(p: Params, delivered_kwh: float) -> float:
    if p.ev_capacity_kwh <= 0:
        return p.ev_soc_pct
    return p.ev_soc_pct + delivered_kwh / p.ev_capacity_kwh * 100
