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
   discharge at those powers) and one EV charge level: off, spare solar only,
   or 6/16/32 A (a fixed level also takes any spare solar above it).
   The plan is the cheapest one that has the battery and the car at their
   targets (80% by default) when the afternoon peak price starts (15:00 by
   default, when Amber steps up from off-peak). Peak and seasonal pricing come
   through in the Amber price itself, so after the deadline the plan simply
   avoids the grid because it is dear.
   Being short of a target costs $20/kWh in the plan, so it is only missed
   when that is physically impossible.
3. In **Auto** mode it sends the first slot to PowerSync
   (`force_charge`, `force_discharge`, `hold_battery_soc`, `restore_normal`)
   and to the charger (`switch.charger_charge_control`,
   `number.charger_maximum_current`). In **Plan only** mode it just shows the
   plan.

## Spare solar first

Grid energy for the car or the battery is only bought for what the sun can't
supply by the deadline:

- The car charges on spare solar: in Auto the charger current is matched to
  what would otherwise be exported (the car's draw minus grid import) every
  30 seconds. Below the charger's 6 A minimum the car pauses, after riding out
  a short cloud at 6 A for up to 3 minutes, and it rests at least 5 minutes
  before starting again. The battery fills from the sun first; the car gets
  the rest.
- With the default solar setting, **Both (10% and 50%)**, the plan is worked
  out for a dull day (Solcast's 10% forecast) and a typical day (the median),
  and each step is the one with the lowest average cost across the two. The
  dull day's targets must still be met, so a top-up that can't wait is still
  bought in time, but grid energy that a typical day's sun would provide isn't
  bought early just because the grid is a fraction of a cent cheaper now. The
  same rule stops a morning sale that a dull afternoon would have to buy back.
  The plan shown on the dashboard is the dull day's, so its "at peak" figures
  are the safe ones and its grid top-ups shrink as the sun turns up.
- Both forecasts start from the panels' live output and fade back to the
  forecast over the next few hours.
- At an import price of 0c, grid and solar cost the same, so either is used.

Selling: the plan sells battery energy (force discharge) only when the feed-in
price beats the value of keeping it, after battery wear (5 c/kWh by default)
and losses, and never below what's needed to meet the target at the peak.
Turn `switch.energy_planner_allow_selling_to_grid` off to stop selling.

The battery never feeds the car: while the car charges, the existing
`ev_grid_only_charging` rule holds the battery, and where that rule doesn't
(it only starts from normal mode, so not straight after one of the planner's
own commands) the planner holds it itself. It never plans a force discharge
while the car charges.

Grid import (house + battery charging + car) is kept under the grid import
limit (14 kW by default), which also keeps the Ocular charger's own load
management from throttling the car.

## Price spikes

The live 5-minute Amber price is a slot of its own at the start of every plan,
and the planner re-plans as soon as PowerSync's current price changes, so an
unforecast feed-in spike is sold into within about a minute and the selling
stops when it ends. Forecast intervals that Amber flags as a potential spike
are planned on the dear end of Amber's range (high import, best feed-in), so
the battery is kept ready for them.

## Alerts

In Auto, if the plan can't be carried out for about 15 minutes (three
retries), a notification goes to `notify.mobile_app_poco_x3_gt`, once per
incident, with a second "Sorted now" message when it clears. It covers:

- PowerSync in a mode the planner didn't ask for (your own manual command,
  another automation, the app), except the EV grid-only hold.
- PowerSync not accepting the planner's command.
- The battery not following a forced charge, sale or hold (checked from the
  SoC over 10 minutes).
- The car not charging when planned: the charger switch stays off, the car
  draws nothing (check the car's own timer), or the charger was changed by hand.

`binary_sensor.energy_planner_plan_blocked` is on while an alert is active.
Nothing is sent in Plan only.

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
| `time.energy_planner_peak_price_starts` | The deadline for the targets |
| `number.energy_planner_battery_target_at_peak` | Battery % wanted at the peak |
| `number.energy_planner_ev_target_at_peak` | Car % wanted at the peak (capped at the charge limit set in the car) |
| `number.energy_planner_ev_state_of_charge_manual` | Car SoC to use while the MINI's CarData SoC sensor is unavailable; it counts up from charger power |
| `number.energy_planner_ev_battery_size` | Usable car battery, kWh |
| `number.energy_planner_battery_reserve` | Never plan below this % (0 by default: the battery normally runs right down). Above 0, Auto holds the battery at this level |
| `number.energy_planner_battery_wear_cost` | c/kWh charged for every kWh taken out of the battery |
| `number.energy_planner_grid_import_limit` | kW |
| `select.energy_planner_solar_forecast` | Both (10% and 50%), the default, weighs a dull day against a typical one (see Spare solar first). Expected (50%) or Cautious (10%) plan on one forecast only |
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
