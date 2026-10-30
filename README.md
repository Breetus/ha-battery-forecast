# Energy Planner

A local optimiser for the house battery (Sungrow SH10RS, 20 kWh) and the EV,
running as a Home Assistant custom integration. It replaces PowerSync's paid
optimiser but keeps PowerSync for everything else: PowerSync still owns the
single Modbus connection to the inverter, fetches Amber prices and Solcast
forecasts, and carries out the commands.

## What it does

Every 5 minutes, and at the start of each half hour, it:

1. Reads Amber import and feed-in prices (PowerSync's Amber data, falling
   back to the Amber Express sensors), the Solcast forecast (PowerSync's copy,
   falling back to the Solcast sensors), a half-hourly house load profile from
   the last 10 days of history (the house load sensor doesn't include the
   car), the battery SoC, and the car's SoC and charge limit from CarData.
2. Plans the next ~40 hours in half-hour slots. In each slot it picks one
   battery mode (normal, hold, force charge at 2.5/5/7.5/10 kW, or force
   discharge at those powers) and one EV charge level (off, 6 A, 16 A, 32 A).
   The plan is the cheapest one that has the battery and the car at their
   targets (80% by default) when the evening peak starts (16:00 by default).
   Being short of a target costs $20/kWh in the plan, so it is only missed
   when that is physically impossible.
3. In **Auto** mode it sends the first slot to PowerSync
   (`force_charge`, `force_discharge`, `hold_battery_soc`, `restore_normal`)
   and to the charger (`switch.charger_charge_control`,
   `number.charger_maximum_current`). In **Plan only** mode it just shows the
   plan.

Demand charge: the network charges for the month's highest half-hour grid
import inside the evening demand window (16:00-21:00 by default; Amber's own
demand-window flags are used when present). The planner keeps track of this
month's peak, and in the window it charges the full demand rate for any import
above it, so the plan covers the window from the battery and charges the car
before it starts. Set the rate in `number.energy_planner_demand_charge`
($/kW; copied from PowerSync's demand-charge settings when they're on).

Selling: the plan sells battery energy (force discharge) only when the feed-in
price beats the value of keeping it, after battery wear (5 c/kWh by default)
and losses, and never below what's needed to meet the target at the peak.
Turn `switch.energy_planner_allow_selling_to_grid` off to stop selling.

The battery never feeds the car: while the car charges, the plan assumes the
existing `ev_grid_only_charging` rule holds the battery, and it never plans a
force discharge at the same time.

Grid import (house + battery charging + car) is kept under the grid import
limit (14 kW by default), which also keeps the Ocular charger's own load
management from throttling the car.

## Manual control

PowerSync's own force charge, force discharge, hold and restore buttons keep
working. When PowerSync is in a forced mode that Energy Planner didn't ask for,
Auto steps aside (status shows "Manual … in control") until that command ends.
If you switch the charger on or off or change its current from the UI, Auto
leaves the charger alone until the car is unplugged.

## Entities

| Entity | Purpose |
|---|---|
| `select.energy_planner_mode` | Auto or Plan only (starts in Plan only) |
| `time.energy_planner_peak_price_starts` | Start of the demand window, and the deadline for the targets |
| `time.energy_planner_peak_price_ends` | End of the demand window |
| `number.energy_planner_demand_charge` | Network demand charge, $/kW of the month's peak |
| `sensor.energy_planner_peak_demand_this_month` | Highest half-hour import in the window so far this month |
| `number.energy_planner_battery_target_at_peak` | Battery % wanted at the peak |
| `number.energy_planner_ev_target_at_peak` | Car % wanted at the peak (capped at the charge limit set in the car) |
| `number.energy_planner_ev_state_of_charge_manual` | Car SoC to use while the MINI's CarData SoC sensor is unavailable; it counts up from charger power |
| `number.energy_planner_ev_battery_size` | Usable car battery, kWh |
| `number.energy_planner_battery_reserve` | Never plan below this % |
| `number.energy_planner_battery_wear_cost` | c/kWh charged for every kWh taken out of the battery |
| `number.energy_planner_grid_import_limit` | kW |
| `switch.energy_planner_allow_selling_to_grid` | Allow force discharge for profit |
| `switch.energy_planner_allow_grid_charging` | Allow force charge from the grid |
| `switch.energy_planner_control_ev_charger` | Let the planner switch the charger |
| `sensor.energy_planner_status` | What it is doing now, and why |
| `sensor.energy_planner_plan_now` | Current step; the whole plan is in the `steps` attribute |
| `sensor.energy_planner_next_change` | The next planned change and when |
| `sensor.energy_planner_battery_at_peak`, `sensor.energy_planner_ev_at_peak` | Expected % when the peak starts |
| `sensor.energy_planner_plan_cost` | Expected net grid cost over the plan |

`dashboard/energy_planner_view.yaml` is a ready-made dashboard view (the chart
needs apexcharts-card from HACS).

## Install

1. Copy `custom_components/energy_planner` to `/config/custom_components/`.
2. Restart Home Assistant.
3. Settings > Devices & services > Add integration > Energy Planner. The form
   is pre-filled with this house's entities; check them and submit.
4. Leave it in Plan only for a day and compare the plan with what you would
   have done, then switch to Auto.

## Development

The planner (`planner.py`), the input parsing (`forecast.py`) and the command
logic (`control.py`) have no Home Assistant imports.

```
pip install pytest-homeassistant-custom-component numpy
pytest
```
