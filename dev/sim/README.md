# shelly_sim — simulated Shelly EM Gen4 (DEV ONLY)

A Home Assistant custom component that pretends to be the house's Shelly EM Gen4
with two CT clamps, so a **sandbox** Home Assistant has realistic live readings and
months of history for demoing `electricity_analyzer` before real data is wired in.

Never install it on a real Home Assistant: it writes long-term statistics under
ordinary `sensor.*` entity_ids.

## What you get

A parent device **Shelly EM Gen 4** (Shelly EM Gen4, `S4EM-002CXCEU`) with two
sub-devices, each with the same eight sensors the real integration creates:

| device | clamp | entity_id prefix |
|---|---|---|
| Full House Energy Monitor | 120 A on the mains (house total) | `sensor.full_house_energy_monitor_` |
| Hall Energy Monitor | 50 A on the aquarium circuit (inside the total) | `sensor.hall_energy_monitor_` |

Suffixes: `power` (W), `apparent_power` (VA), `power_factor` (0..1), `voltage` (V),
`current` (A), `energy` (kWh, total_increasing), `energy_returned` (always 0),
`frequency` (Hz).

The readings come from `model.py`, a deterministic load model calibrated to
sample Irish smart-meter data (monthly kWh/day, weekday factors, hourly
shape for Jan/Apr/Jul/Oct, Europe/Dublin local time). The hall circuit runs pumps,
two cycling thermostatic heaters and timed LEDs; the rest of the house has a
smooth base plus kettle, cooking, laundry, shower and fridge events. CT
calibration (+2 % hall, −0.5 % house) and slightly unaligned channel readings
make the hall reading occasionally exceed the house reading — a handful of times
a week outside winter, by tens of watts, mostly at night — as on the real system.

## Enable

```sh
cp -r dev/sim/custom_components/shelly_sim <sandbox-config>/custom_components/
```

```yaml
# configuration.yaml
shelly_sim:
  history_days: 400     # optional; hourly statistics to backfill (0 = none)
  update_interval: 5    # optional; seconds between readings
  seed: 42              # optional; same seed = same simulated house
```

Restart Home Assistant. On first start it imports `history_days` of hourly
long-term statistics for all 16 sensors (≈80 s for 400 days; the Energy sensors
show `unknown` until it finishes) and the live energy counters continue from the
imported ones, so the Energy dashboard has no spike or negative step at the join.
Remove the YAML block and restart to remove the simulated devices (statistics
stay).

Run `python model.py` inside the component folder for a quick calibration summary.

## Behaviour worth knowing

* **Restarts.** The energy counters are restored and fast-forwarded over the
  downtime, like a real Shelly that kept counting. Energy used while Home
  Assistant was down therefore appears in the first hour after it comes back.
* **Import once.** A marker in `.storage/shelly_sim.history` stops the import
  running again, and it is skipped whenever the Energy sensors already have
  statistics. To rebuild in a sandbox: stop HA, delete that file and the
  recorder database (`home-assistant_v2.db*`), start HA. An interrupted import
  (HA stopped mid-way) is redone automatically on the next start.
* **Changing the seed** later changes the live readings but not the imported
  history.
