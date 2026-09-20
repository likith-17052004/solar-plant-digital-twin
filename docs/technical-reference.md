# Technical reference

The full, step-by-step build of this project: what each model is, which
published paper it comes from, how it was validated, and — at least as
important — what it does not claim to do.

For the short version, see the [README](../README.md).

> **Note on the physics, added after this document was written.** The models
> below were originally implemented here by hand, from their published papers,
> under a stdlib-only constraint. They now come from **pvlib** instead. The
> derivations, citations and stated limits are all still accurate — what
> changed is who executes the equation.
>
> Three things genuinely improved and are *not* described below: solar position
> moved from PSA to NREL's SPA and gained atmospheric refraction; the clear-sky
> model moved from Haurwitz plus a fixed 15% diffuse fraction to Ineichen-Perez,
> which gives a physically varying diffuse share; and transposition moved from
> isotropic to Perez. One term that was documented here as omitted — the sky
> each row masks from its neighbour — now exists, and measures 0.22% rather
> than the "few percent" guessed at below.
>
> Every other model was checked against pvlib and matched exactly. See the
> README's *On the physics* section and `tests/test_pvlib_agreement.py`.

---

Personal hobby project: a fictional 100 MW AC solar plant. The backend includes
a validated plant model, steady-state available DC power, conditional
inverter-terminal AC output, real site weather transposed to plane-of-array
irradiance, DC string voltage/current feasibility checks, and net export at
the point of interconnection after grid-side losses — plus an interactive 3D
frontend driven live by that same backend. Export controls, SCADA, storage,
forecasts, and a public API are future steps.

## Run

Python 3.10+; no third-party packages required. From this directory:

```sh
python3 -m solar_twin
python3 -m solar_twin --topology
python3 -m solar_twin --simulate examples/dc_scenarios.json
python3 -m solar_twin --simulate-ac examples/ac_scenarios.json --inverter-model config/inverter_model.json
python3 -m solar_twin --simulate-electrical examples/electrical_scenarios.json
python3 -m solar_twin --simulate-grid examples/grid_scenarios.json --grid-loss-model config/grid_loss_model.json
python3 -m solar_twin --simulate-weather 2024-06-01 2024-06-02
python3 -m solar_twin --simulate-weather 2024-06-01 2024-06-02 --ac --inverter-model config/inverter_model.json
python3 -m unittest discover -s tests -v
```

`--simulate-weather` fetches real historical hourly weather over the network
(no API key required); the other modes run fully offline.

```sh
python3 -m solar_twin.server
```
Then open `http://127.0.0.1:8000` for the interactive 3D frontend (Step 7
below). This is a separate entry point from the CLI above — serving is a
different concern from one-shot scenario evaluation.

Edit `config/plant.json` to change the design. Invalid configurations exit with
a nonzero status. Python consumers can use `solar_twin.plant.load_plant(path)`.
All quantities use units in field names. Temperature coefficients are fractions
per degree C, not percentages. Topology IDs are stable for a given layout.

## Baseline

| Item | Configuration |
| --- | --- |
| DC nameplate at standard test conditions (STC) | 130.065 MWp |
| Installed inverter active power limits | 20 × 5 MW = 100 MW |
| Point of interconnection export limit | 100 MW |
| DC/AC ratio | 1.30065 |
| PV modules | 200,100 × 650 W |
| Strings | 29 modules in series; 345 parallel strings per block |
| Block transformers | 20 × 6.3 MVA, 0.66/33 kV |
| Grid transformer | 125 MVA, 33/220 kV |
| Mounting assumption | Fixed tilt 25°, azimuth 180° (south) |
| Site | Pavagada, Karnataka, India (approximate town-center coordinates) |

Topology: module groups → strings → block inverter → block transformer →
33 kV collector bus → grid transformer → point of interconnection.
DC combining, feeder circuits, switchgear, and protection are not yet detailed.
No real plant connection or real telemetry is present.

## Equipment references and assumptions

Module ratings are from the 650 W STC column of
[Trina TSM-DEG21C.20, 2024 A](https://www-cdn.trinasolar.com/wwwstorage/sites/10/Datasheet_Vertex_DEG21C.20_EN_2024_A.pdf):
37.7 V Vmp, 45.5 V Voc, 17.27 A Imp, 18.35 A Isc; Voc coefficient
−0.25%/°C, power coefficient −0.34%/°C, Isc coefficient +0.04%/°C, and
1500 V maximum system voltage.

Inverter ratings reference
[Sungrow SG5000UD-20, V1.2.2](https://en.sungrowpower.com/upload/documentFile/DS_SG5000UD%20SG5000UD-20%20Datasheet_V1.2.2_EN.pdf.pdf):
1500 V maximum DC voltage, 960–1300 V nominal-power MPPT range, 6112 A
maximum operating DC current, 10000 A short-circuit current, 660 V AC,
and 5000 kVA at 50°C. We impose a 5 MW active limit per block; full 5 MW
at that apparent-power rating assumes unity power factor. Step 3 includes an
assumed temperature derating curve; reactive-power operation remains future work.

Transformer sizes, topology, orientation, and design margins are project
assumptions, not specifications of an existing installation. The site
coordinates (Step 4) are an approximate town-center stand-in for a real
solar-park region, not a surveyed plant boundary.

## What validation establishes

Checks cover positive finite quantities, integer equipment counts, total
capacity, orientation, transformer ratings at unity power factor, STC string
voltage, cold open-circuit voltage, and aggregate inverter input currents.

The provisional cold-cell assumption is −10°C, with a separate 3% voltage
margin. Corrected string Voc including that margin is 1478.00 V, below 1500 V.
This is a conditional baseline: a sufficiently colder site or larger voltage
allowance requires shorter strings and resizing. The site design temperature must be
reviewed before connecting weather. These margins are assumptions, not a
claimed electrical-code sizing procedure.

Operating current allows a 1.02 multiplier (6077.31 A per block); the
short-circuit check uses 1.25 (7913.44 A). These do not guarantee unrestricted
operation under stronger irradiance or rear-side contribution. Bifacial gain
is not included in DC nameplate. There is only about 2.6% operating-current
headroom above STC; the simulation must model current clipping, including
rear-side contribution. The 1.02 multiplier is a static check envelope, not a
prediction of peak field current.

The STC MPPT check does not establish hot-cell or low-light voltage capability.
Temperature-dependent voltage/current, transformer and cable losses,
auxiliaries, availability, and export controls remain future
physics steps. Inverter nameplate and export limit are separate: net
export will be lower than inverter output after losses and auxiliary demand.
This static model is not a construction design or a calibrated operating twin.

## Step 2: sunlight and temperature to DC power

```python
from solar_twin.plant import load_plant
from solar_twin.dc import Conditions, simulate_dc

plant = load_plant("config/plant.json")
result = simulate_dc(plant, Conditions(
    poa_irradiance_w_m2=1000,
    air_temperature_c=35,
    wind_speed_10m_m_s=2,
))
print(result.cell_temperature_c)   # 65.63 C
print(result.plant_available_dc_mw)  # 112.10 MW DC potential
```

The input is **front-side plane-of-array (POA) irradiance**, the sunlight
incident on the tilted panels. It is not horizontal irradiance (GHI), daily
solar energy, or cloud percentage. Step 4 below produces this input from a
real site location, solar position, and weather transposition; the scenarios
here remain hand-written and synthetic, and the STC example explicitly fixes
cell temperature to 25°C.
Ambient air at 25°C does not make sunlit cells operate at 25°C.

The [Sandia temperature model](https://pvpmc.sandia.gov/modeling-guide/2-dc-module-iv/module-temperature/sandia-module-temperature-model/)
estimates back-surface temperature as `T_air + POA * exp(a + b * wind)`.
The [cell-temperature correction](https://pvpmc.sandia.gov/modeling-guide/2-dc-module-iv/cell-temperature/sandia-cell-temperature-model/)
adds `POA / 1000 * delta_T`. Defaults are representative open-rack glass/glass
parameters: `a=-3.47`, `b=-0.0594`, `delta_T=3°C`, with **wind measured at 10 m**.
These are exposed through `ThermalParameters` and recorded in results; they are
not fitted to this specific Trina module. See the
[pvlib reference and numerical example](https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.temperature.sapm_cell.html).
A provided cell temperature bypasses thermal estimation and is marked in output.

The [PVWatts v5 DC equation](https://pvwatts.nrel.gov/downloads/pvwattsv5.pdf)
is `P_dc = P_stc * G_effective / 1000 * (1 + gamma * (T_cell - 25))`.
Here `gamma=-0.0034/°C`, from the selected module; output is floored at zero.
This implements that DC equation, not the complete PVWatts application or
latest PVWatts model. The v5 equation keeps low-light response linear; it does
not use the older quadratic correction. The model's
[pvlib documentation](https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.pvsystem.pvwatts_dc.html)
provides the same equation and reference conventions.

Optional soiling multiplies incident irradiance by `1 - soiling_loss_fraction`
once for electrical output. Thermal input stays at incident POA. This is a
uniform optical approximation; temperature changes caused by dirt, partial
shading, mismatch, spectrum, incidence-angle losses, and bifacial rear gain
are not included. Defaults assume clean panels with no other hidden losses.

Results contain available module power, each block's DC potential, plant DC
potential, temperatures, model parameters, and condition warnings. All blocks
receive the same conditions in this step. Power may exceed 100 MW because
inverter voltage/current clipping, AC conversion, and export control have
not been applied to the DC result. Step 3 adds AC conversion separately.
No DC voltage/current or grid export is claimed by this DC model.
The existing static electrical checks still run when loading the plant.

These are independent steady-state snapshots, with no elapsed time or energy
integration. They are not a second-by-second thermal transient model for SCADA.
Cold-cell conditions below the plant design minimum and temperatures outside
the reference module's −40 to 85°C range emit warnings rather than silently
pretending the inverter shut down. Warnings mark extrapolation, not validated
equipment operation. Runtime input bounds (POA 0–2000 W/m², air −60 to 70°C,
wind 0–75 m/s, provided cell temperature −60 to 120°C) are software envelopes,
not statements of model accuracy throughout those ranges. NaN, infinity,
boolean values, and invalid loss fractions are rejected.

Tests cover a published temperature example, STC nameplate, the module's
temperature coefficient, low light, night, wind cooling, soiling, block totals,
input validation, and the runnable JSON scenario interface.

## Step 3: inverter-terminal AC

```python
from solar_twin.ac import simulate_ac

ac = simulate_ac(plant, Conditions(1000, 35, 2))
print(ac.plant_inverter_ac_mw)  # 100 MW at inverter terminals
hot = simulate_ac(plant, Conditions(1000, 35, 2), inverter_air_temperature_c=55)
print(hot.plant_inverter_ac_mw)  # 90 MW under the assumed thermal curve
```

`--simulate-ac` accepts the same named weather scenarios as `--simulate`.
AC scenarios may also supply `inverter_air_temperature_c`, separately from
panel/weather inputs. By default inlet air equals ambient weather air, never
the much hotter solar cell temperature. The result records which was used.
`config/inverter_model.json` contains the default coefficients; editing this file
changes a run when it is explicitly passed with `--inverter-model`. Without that
option the dataclass defaults apply. Python callers can pass `InverterParameters`.

The [PVWatts inverter curve](https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.inverter.pvwatts.html)
provides load-dependent efficiency:

```text
Pdc_reference = nominal_AC / eta_nominal
zeta = DC / Pdc_reference
eta = eta_nominal / 0.9637 * (-0.0162*zeta - 0.0059/zeta + 0.9858)
```

The implementation rearranges `eta * DC` to avoid division by zero, floors
negative conversion to zero, and caps efficiency at the configured maximum.
Inverter DC reference power is derived from its AC rating, not the array's
130.065 MWp nameplate. The array can provide more power than the inverter takes.

The [Sungrow reference datasheet](https://en.sungrowpower.com/upload/documentFile/DS_SG5000UD%20SG5000UD-20%20Datasheet_V1.2.2_EN.pdf.pdf)
lists 99.0% maximum and 98.7% European weighted efficiency. We use 98.7% as
an **assumed nominal proxy** for a generic curve; European weighted efficiency
is not the full-load efficiency or a measured curve fit. The 99% ceiling avoids
exceeding the published maximum. The datasheet also gives −35 to 60°C operating
ambient range, with derating above 50°C for SG5000UD-20.

The detailed hot-weather curve and shutdown/restart logic are not established
by that datasheet. The configurable project assumption retains the imposed
5 MW cap through 50°C, then reduces capacity by **2% of rating per additional
degree**: 4.5 MW at 55°C and 4 MW at 60°C per block. It sets output to zero below
−35°C or above 60°C. Boundary temperatures themselves remain in range. This is
an illustrative continuation, not a certified manufacturer curve. It has no
hysteresis, cooling dynamics, overload operation, or reactive-power demand.
The 5 MW operational cap applies even where the datasheet permits higher
apparent power at cooler temperatures.

Power accounting distinguishes **unharvested DC potential** from conversion
losses. When AC is limited, a scalar solve estimates the DC power required for
that AC output using the same efficiency curve. This is an off-MPP power
approximation; it does not solve the actual array voltage/current operating
point. Each block and plant satisfy:

```text
available_DC = accepted_DC + unharvested_DC
accepted_DC = inverter_AC + conversion_loss
```

The AC-equivalent reduction fields are diagnostics: nameplate clipping is
applied first, then temperature reduction. Do not add these to the DC power
budget; they describe different units of lost opportunity along the conversion
path. Standby/shutdown accepts zero DC in this model; auxiliary draw is deferred.
`operating_efficiency` is null when the inverter accepts no power.

**Output remains conditional on DC voltage/current and MPPT feasibility.**
The PVWatts power model cannot check hot-string voltage, startup voltage,
input current clipping, or cold overvoltage from power alone. Step 5 below
adds exactly those checks as a separate diagnostic layer (`evaluate_dc_electrical`)
rather than fabricating them here; this AC result does not include them.
Every AC result includes this limitation in `assumptions` and preserves the
DC temperature warnings. It also records the generic curve and thermal
assumptions. Module warnings are not inverter protection trips.

AC totals are at inverter terminals, at unity power factor. Transformer/cable
losses, auxiliary loads, and point-of-interconnection export controls are not
yet applied here — Step 6 below adds those on top of this result, and that is
where changing the plant export limit finally changes what reaches the grid.
All 20 blocks share conditions; no real SCADA is connected.

Tests check nominal and partial-load conversion, zero and low-light operation,
clipping, thermal boundaries, configurable derating, panel/inverter temperature
separation, block totals, monotonic output, and power conservation across a
range of input powers and temperatures. Both DC and AC CLI modes are covered.

## Step 4: site location, solar position, and real weather

```python
from solar_twin.weather import fetch_hourly_weather
from solar_twin.timeseries import simulate_weather_series

observations = fetch_hourly_weather(plant.location, "2024-06-01", "2024-06-01")
snapshots = simulate_weather_series(plant, observations)
print(snapshots[6].result.plant_available_dc_mw)  # midday DC potential, from real weather
```

`config/plant.json` now includes a `location` block: latitude, longitude,
elevation, IANA timezone, and ground albedo. The chosen site is **Pavagada,
Karnataka, India**, an approximate town-center coordinate standing in for the
real-world Pavagada Solar Park region — not a surveyed boundary of this
fictional plant. `Location` validation rejects out-of-range coordinates and
unresolvable timezones the same way `Plant.validate()` already rejects bad
equipment ratings.

**Solar position** (`solar_twin/solar_position.py`) uses the
[Blanco-Muriel et al. (2001) PSA algorithm](https://doi.org/10.1016/S0038-092X(00)00156-0)
("Computing the solar vector," Solar Energy 70(5)): a compact, closed-form
calculation (~0.01° accuracy) with no ephemeris tables, implemented directly
in the same spirit as the Sandia temperature model and PVWatts v5 equation in
Step 2. It requires a timezone-aware UTC timestamp. The original paper
documents accuracy over 1999-2015 (a 2020 revision extends this); dates
outside that window still compute but are marked
`date_outside_psa_validity_window`, the same extrapolation-warning idiom used
for cell temperature. Zenith ≥ 90° means the sun is below the horizon. Near
the tropics in summer the sun can pass north of zenith at solar noon — that is
correct solar geometry when declination exceeds latitude, not a bug.

**Transposition to plane-of-array** (`solar_twin/irradiance.py`) uses the
isotropic sky-diffuse model (Liu & Jordan, 1963 — the same model as
`pvlib.irradiance.get_total_irradiance(model="isotropic")`): beam by angle of
incidence, sky diffuse assumed uniform across the sky dome, and ground-reflected
diffuse by albedo. It omits circumsolar and horizon-brightening effects that
anisotropic models (e.g. Perez) add; this is a documented simplification. On a
horizontal surface, POA is definitionally equal to GHI — this identity is a
test, not just a sanity check.

**Weather** (`solar_twin/weather.py`) fetches hourly direct-normal, diffuse,
and global irradiance plus air temperature and wind from the free, keyless
[Open-Meteo Historical Weather (Archive) API](https://open-meteo.com/en/docs/historical-weather-api)
over stdlib `urllib` — no new dependency, but `--simulate-weather` does need
network access (every other CLI mode remains fully offline). This is
reanalysis/model data, not ground-truth pyranometer measurement, and the
archive typically lags real time by several days.

**Wiring** (`solar_twin/timeseries.py`) feeds each hour's transposed POA
irradiance, air temperature, and wind into the existing, unmodified
`simulate_dc`/`simulate_ac` from Steps 2-3. Each timestamp stays an
independent steady-state snapshot: turning this time series into energy
(MWh) via integration is still a distinct, future step, not something a list
of snapshots implies on its own.

`--simulate-weather START END` (dates inclusive, `YYYY-MM-DD`, UTC) prints one
scenario per hour with its timestamp, solar position, POA breakdown, and DC
result; add `--ac` (optionally with `--inverter-model`) to run the inverter
model instead.

Tests cover solar position at a known reference geometry (solar noon zenith
matching latitude minus declination), the PSA validity-window warning, the
horizontal-surface POA/GHI identity, a hand-worked tilted-surface transposition
example, weather-API parsing and error handling (mocked, no live network calls
in the suite), and the `--simulate-weather` CLI path including the `--ac`
mode.

## Step 5: DC voltage, current, and MPPT feasibility

```python
from solar_twin.dc_electrical import evaluate_dc_electrical

hot = evaluate_dc_electrical(plant, Conditions(1000, 35, 2, cell_temperature_override_c=85))
print(hot.string_vmp_v)   # 929.31 V - below the inverter's 960 V MPPT minimum
print(hot.warnings)       # (..., 'string_voltage_below_mppt_range')
```

Steps 2-4 model DC/AC **power** only, which cannot see hot-string
undervoltage, cold overvoltage, or inverter current-limit conditions -
those require an actual operating voltage/current. `evaluate_dc_electrical`
(`solar_twin/dc_electrical.py`) layers that onto the existing, unmodified
`simulate_dc` (same wrapping pattern `simulate_ac` already uses), **without
changing the DC power number**: this is a diagnostic layer that flags
electrical infeasibility rather than guessing what an inverter would actually
do about it (shut down, limit power, trip protection). That remains future
work, same as full startup-voltage and IV-curve modeling.

String Vmp and Voc scale with the module's published Voc temperature
coefficient; operating current scales with the module's published Isc
temperature coefficient (+0.04%/°C, from the same
[Trina TSM-DEG21C.20](https://www-cdn.trinasolar.com/wwwstorage/sites/10/Datasheet_Vertex_DEG21C.20_EN_2024_A.pdf)
datasheet already cited for Voc/Pmax) and linearly with effective irradiance.
**Assumption**: Vmp's own temperature coefficient isn't separately published,
so it is assumed equal to Voc's; Imp's is assumed equal to Isc's. Both are
project approximations, not manufacturer-measured Vmp/Imp coefficients.
These diagnostic voltages ignore irradiance dependence except at zero light,
and their voltage-current product is not constrained to the PVWatts power.
They must not be treated as a solved IV operating point.

Four conditions are flagged in `warnings`, on top of the DC warnings already
inherited from Step 2:
- `string_voltage_below_mppt_range` / `string_voltage_above_mppt_range` -
  checked only while producing (an inverter isn't tracking MPPT with nothing
  to track).
- `open_circuit_voltage_exceeds_system_limit` - checked under illumination.
  Cold illuminated modules can exceed the DC voltage limit; at zero light,
  photovoltaic voltage and current are zero. Static cold-design checks remain
  separate from the weather snapshot.
- `operating_current_exceeds_inverter_limit` - checked only while producing.

`--simulate-electrical JSON` evaluates named scenarios the same way
`--simulate`/`--simulate-ac` do;
[examples/electrical_scenarios.json](examples/electrical_scenarios.json)
has one scenario per warning condition plus a clean STC and a clean-night
case. This is not yet wired into `--simulate-weather`; that integration is a
clearly-flagged follow-up, not something this step claims to cover.

Tests recover the hand-calculated STC Vmp/Voc/current with no warnings,
confirm each example scenario trips exactly its intended warning and no
others, confirm night reports zero photovoltaic voltage and current, confirm Step 2's own warnings are preserved, and cover the
`--simulate-electrical` CLI path.

## Step 6: grid-side losses and net export at the point of interconnection

```python
from solar_twin.grid import simulate_grid_export

result = simulate_grid_export(plant, Conditions(1000, 35, 2))
print(result.ac.plant_inverter_ac_mw)  # 100.00 MW at inverter terminals
print(result.net_export_mw)            # 97.58 MW after transformer/cable losses and aux load
```

`evaluate_dc_electrical` and `simulate_ac` stop at inverter terminals.
`simulate_grid_export` (`solar_twin/grid.py`) layers block-transformer,
collector-cable, grid-transformer, and HV-cable losses plus auxiliary
(parasitic) load on top of the existing, unmodified `simulate_ac` - the same
wrapping pattern `ac.py` already uses over `dc.py` - to produce **net export
at the point of interconnection (POI)**. This is the first step where
`layout.export_limit_mw` does anything: net export is capped there, with the
difference reported as `curtailment_mw`.

**Unlike the module and inverter, this project cites no real transformer or
cable product** - the Baseline table already calls transformer sizing a
project assumption, not a specification. So `GridLossParameters` uses
generic, industry-typical two-parameter transformer loss ratios (no-load +
load-dependent, ~99.2% full-load block-transformer efficiency, ~99.6% for the
larger grid transformer) and flat MV/HV cable-loss percentages - explicitly
less-grounded than the cited Trina/Sungrow figures used elsewhere. Defaults
live in the `GridLossParameters` dataclass and are mirrored in
[config/grid_loss_model.json](config/grid_loss_model.json), overridable via
`--grid-loss-model`, the same pattern `InverterParameters`/
`config/inverter_model.json` already established.

No-load transformer losses and the auxiliary load are applied **only while
producing AC** (`plant_inverter_ac_mw > 0`); at night every loss/auxiliary
field is exactly zero. This mirrors Step 3's own choice to defer inverter
standby draw ("auxiliary draw is deferred") rather than introduce an
inconsistent day/night boundary. Real 24/7 station-service consumption from
the grid at night remains future work, not modeled here.

Each transformer stage uses the same two-parameter formula:
`loss = no_load_loss_fraction * rated_MVA + load_loss_fraction * rated_MVA * (loading)^2`,
floored at zero (`block_transformer_no_load_loss_exceeds_input` /
`grid_transformer_no_load_loss_exceeds_input` warn instead of going negative
at very low output - the same floor-with-warning idiom `dc.py`/`ac.py` use).
Cable losses are flat fractions of the power flowing at that point; there is
no conductor length or gauge in this model. A real, verified effect of this
loss shape: **no-load losses are a larger fraction of output at partial load
than near nameplate** (measured ~3.1% total loss at 250 W/m² vs. ~2.4% at
1000 W/m² with the shipped baseline config) - a genuine property of
constant-plus-quadratic loss curves, not an artifact.

With the shipped baseline (inverter nameplate == export limit == 100 MW),
peak net export lands a little under 100 MW once losses apply, so
`export_limit_mw` doesn't bind under normal weather in the bundled examples;
curtailment is real and tested, but via a lower export limit
(`dataclasses.replace`), not a fabricated baseline config.

`--simulate-grid JSON` evaluates named scenarios the same way `--simulate-ac`
does (same optional `inverter_air_temperature_c`, same `--inverter-model`);
[examples/grid_scenarios.json](examples/grid_scenarios.json) has night,
partial-load, and near-nameplate scenarios. Not yet wired into
`--simulate-weather` - a clearly-flagged follow-up.

Tests recover the full loss chain from hand-calculated values at nameplate
AC, confirm night zeroes every loss/auxiliary field, confirm a lowered export
limit curtails net export and flags it, confirm very low output floors the
transformer stages without going negative, confirm the partial-load-vs-
nameplate relative loss claim above, confirm Step 3's own warnings are
preserved, validate `GridLossParameters`, and cover the `--simulate-grid` CLI
path including `--grid-loss-model`.

## Step 7: interactive 3D frontend

```sh
python3 -m solar_twin.server
# then open http://127.0.0.1:8000
```

A 3D scene of the plant (20 blocks / 6,900 strings, tilted 25° south-facing,
at Pavagada) with a sun and sky driven by the real solar-position math from
Step 4, and panels that visually reflect what the physics pipeline is
actually computing for the selected moment. Drag the time slider and the
sun moves, the sky shifts from day to dusk to night, and the panels dim -
all backed by real numbers in the metrics panel (POA irradiance, cell
temperature, DC available, AC output, net export, and any active warnings).

**Physics stays server-side, in Python; the browser is a thin renderer.**
`solar_twin/server.py` is a stdlib-only `ThreadingHTTPServer` (no new
dependency, same rule the rest of the backend follows) that serves the
static `frontend/` files and three JSON routes:
- `GET /api/plant` - layout/location for building the 3D array.
- `GET /api/snapshot?date=&hour=` - the synthetic clear-sky path (see below);
  a local round trip is fast enough for slider-drag interactivity, so there
  was no reason to fork the PSA/PVWatts/grid-loss equations into JavaScript
  a second time.
- `GET /api/weather-day?date=` - the **real** path: calls the existing
  `fetch_hourly_weather` (Step 4) unmodified and runs the same combined
  pipeline (`solar_position` → `transpose_to_poa` → `evaluate_dc_electrical`
  → `simulate_grid_export`) per hour, returned as 24 entries to replay
  without one round trip per frame. Because `fetch_hourly_weather` requests
  UTC-calendar-day-aligned data (Step 4's existing behavior, unchanged here),
  the frontend matches each slider position to the closest observation by
  its *own* local hour rather than assuming array index equals local hour.

**`solar_twin/clear_sky.py` is demo support, not a numbered physics step.**
It exists purely so the slider has something physically reasonable to show
instantly, explicitly separated from the real weather path:
- `estimate_clear_sky_ghi`: the **Haurwitz (1945)** clear-sky model -
  `GHI = 1098·cos(Z)·exp(-0.059/cos(Z))` for `Z < 90°` (the same model
  `pvlib.clearsky.haurwitz` implements) - a real, citable equation, not an
  invented curve.
- `synthesize_dni_dhi`: a coarse fixed-15%-diffuse-fraction split, explicitly
  **not** a validated decomposition model (contrast Step 4's real weather,
  which gets DNI/DHI directly from Open-Meteo). Feeds straight into the
  existing, unmodified `transpose_to_poa`.
- `synthesize_air_temperature_c`: a simple diurnal sinusoid (representative
  Pavagada values), also explicitly illustrative, not a forecast.

The scene uses metre-scale rows with module cell textures, reflective glass,
steel supports, access roads, inverter stations, a fenced substation,
transformers, gantries, transmission towers and surrounding terrain. Each of
6,900 instanced tables represents 29 modules (200,100 total). Table footprints
use 1.303 × 2.384 m modules; row spacing and all civil works are illustrative.
This is not a surveyed replica of the existing Pavagada installation.

Aerial, panel-row and substation camera presets, orbit navigation, a day
playback control and a compact data drawer keep the plant visible. Shadows,
lighting and sky follow the backend solar position. Performance mode reduces
pixel resolution and disables shadows. Three.js is loaded from a pinned CDN
version; initial loading needs an internet connection and WebGL support.

Clouds are raymarched from genuine 3D density volumes. Each of two altitude
bands (a low cumulus-like deck, a high cirrus-like one) is baked at startup
into a `Data3DTexture` covering a 10 km × 10 km footprint and that band's
altitude range — roughly 78 m per voxel, about 0.5 MB in total, taking ~70 ms
to generate. Placement uses Worley cells, shaped by a cumulus-like vertical
profile (sharp condensation-level base, softer eroding top) and eroded by 3D
value-noise fbm, all periodic so the tile repeats without a seam. The shader
samples those volumes trilinearly, adds high-frequency noise to keep
silhouettes crisp closer than the voxel scale, and jitters each ray's start
with interleaved gradient noise (Jimenez 2014) to trade step banding for fine
noise. It needs GLSL ES 3.00 (`sampler3D`), so the material is built with
`glslVersion: THREE.GLSL3`, which supplies no `gl_FragColor` — the shader
declares its own output.

Lighting is a single-scattering march rather than a colour lerp: a dual-lobe
[Henyey–Greenstein](https://doi.org/10.1086/144246) phase function (1941) sets
how much light scatters toward the eye, Beer's law along the sun direction
gives the shadowing, and extinction is per-channel so thick cores stay warm.
The phase function is what produces bright rims when looking toward the sun —
without it clouds read as flat grey from every angle. Sun and ambient terms
come from the same lights as the rest of the scene, so clouds darken through
dusk on their own. Leonardo Awen Gonçalves' MIT-licensed three.js
[volumetric-clouds](https://github.com/leoawen/volumetric-clouds) was a useful
reference for how these standard pieces fit together.

**This is the intended seam for real cloud data.** A real 3D cloud field —
model cloud-water content, a radar reflectivity volume — already has exactly
this shape, so filling the same voxel grid from it requires no shader change.
What is *not* real today: the shapes themselves (procedural), the split of one
Open-Meteo `cloud_cover` figure across two heights (the high band is scaled
down from the low one, since the API reports no per-height breakdown), and
drift, which uses the 10 m wind speed along a fixed bearing because the
snapshot carries no wind direction and no cloud-altitude wind. Coverage is the
one genuinely weather-driven input, and clouds still do not cast shadows on
the array or affect the irradiance the physics uses — POA already accounts for
clouds via the measured DNI/DHI, so adding cloud shadows on top would
double-count them. The clear-sky demo reports zero cloud cover, so clouds
appear only once real weather is loaded.

Historical replay fetches adjacent UTC dates and filters to the selected
Pavagada local calendar day before selecting the nearest hourly observation.
The snapshot time is shown in the data drawer; hourly observations are not
interpolated into sub-hourly measurements. Pending slider responses cannot
replace a newer selection. Weather data is reanalysis, not SCADA telemetry.

Review corrections: solar dates now use UTC datetime arithmetic (with a
regression against the published NREL SPA example), weather rejects malformed
or nonfinite values and converts timestamp offsets to UTC, and grid auxiliary
consumption is limited to the generation remaining after upstream losses.
Unserved auxiliary demand is flagged; grid import remains deferred. The
frontend also displays solar-position and transposition warnings. The PSA
1999–2015 validity warning remains; current/future-year accuracy is not
certified by this implementation.

Tests cover the Haurwitz model (peak at zenith 0, zero at/beyond 90°,
monotonic decrease), the DNI/DHI split (recovers the same horizontal-surface
GHI identity Step 4's transposition tests already use), input validation,
and a real HTTP round trip against the server (`/api/plant`, `/api/snapshot`
at local noon and local midnight, malformed-request handling, static file
serving) via a background thread on an ephemeral port - no live network
access needed in the suite.

### Volumetric clouds: making the shape real

The first version of the cloud volume built its density as a 2D footprint
multiplied by one height envelope shared by every column. That is an
**extrusion**: the cross-section is identical at every altitude, so the
silhouette barely changes as you orbit and the cloud reads as flat however
much noise is layered on top. Erosion noise can subtract detail; it cannot
build form. The rewrite makes the shape genuinely three-dimensional:

- **Width varies with height** (`cumulusWidth`) - a flat condensation-level
  base, the widest section about 40% of the way up, then a rounded dome. This
  is the change that turns the volume from a silhouette into a solid.
- **Each convective cell owns its tower**: its own height, lean and strength,
  hashed from the Worley cell id. Weak cells fall below the coverage
  threshold entirely, which breaks up the regular lattice that one-cloud-per-
  cell otherwise paints across the sky.
- **3D billow noise** (inverted Worley) for the cauliflower surface, baked
  once into a small periodic tile and read back trilinearly - 81 hashes per
  sample is far too slow to run per voxel over a million-voxel volume.
- **64 vertical levels** instead of 32, about 23 m per voxel. A cauliflower
  bulge is a 20-50 m feature; at 47 m it was simply averaged away.
- **Adaptive raymarching**: long strides through clear air, short steps on
  contact with cloud, which buys roughly three times the detail inside the
  cloud for the same sample budget.
- **Multiple scattering octaves** (Schneider & Vos, *The Real-time Volumetric
  Cloudscapes of Horizon Zero Dawn*, SIGGRAPH 2015) plus a powdered-sugar
  term. Real clouds are white *because* light bounces many times inside them;
  a single Beer's-law term makes thick cloud dark and muddy no matter how good
  the geometry is.
- **Height-graded fill**: cool skylight from above, a warm soil-bounce tint at
  the base. Without that gradient every voxel gets identical ambient and the
  form flattens out again.

Two colour bugs worth recording, both found by sampling rendered pixels rather
than by eye. Cloud extinction had been made **blue-heavy** to keep thick cores
"warm"; that turns transmitted light yellow, and yellow against blue skylight
lands on green, so every cloud in the scene was faintly olive. Mie scattering
off droplets much larger than the wavelength is near-neutral - that is *why*
clouds are white - so extinction is now `vec3(1.0)`. Separately, mixing blue
skylight and orange ground bounce in equal measure at the cloud base produced
the same green; the bounce is now a 28% tint, not half the fill. Sunlit tops
measure `[207,213,215]` - neutral white.

Honest limits: the field is a **10 km periodic tile**, so it repeats across a
wide sky, and is visible as repetition at high coverage; the two altitude
bands stand in for real per-height cloud data, which Open-Meteo does not break
out, so the high layer is scaled from the single `cloud_cover` figure as a
stated assumption; and **clouds are visual only** - they do not attenuate the
irradiance driving the physics, which comes from the weather feed's own
DNI/DHI/GHI. Baking the volumes costs roughly 0.6-0.8 s once at startup,
behind the loading screen. Clouds appear only on the real-weather path;
"clear sky" means what it says, so the synthetic demo has none.

### Making the sun visible

The sun was there the whole time and could not be seen. Measuring a radial
profile outward from it explained why: the disc rendered at 255/255 and the
sky one degree away at 245/255. **Four percent contrast.** No amount of
brightening a sprite fixes that, because there is nothing above white.

Three things were wrong, and the fix for each was found by reading pixels
rather than by looking:

- **Exposure.** At 0.88 the whole sky sat at the top of the ACES curve, so it
  clipped toward white and left no headroom for anything brighter. Exposure is
  now 0.45, with every scene light scaled by a `LIGHT_GAIN` so the ground does
  not go down with it (measured soil 140/136/96 → 132/122/78, slightly richer
  rather than dimmer). Disc-to-sky contrast went from 10 to 57, and the sky
  recovered its colour: blue-minus-red from 16 to 31.
- **The Mie aureole.** three.js's default `mieCoefficient` of 0.005 throws a
  bright haze halo around the sun that washes the sky within a degree of the
  disc. It is now 0.0022 at high sun, rising again as the sun drops - which is
  what makes a sunset glow, and stands in crudely for the far longer path
  length through the aerosol layer at low elevation.
- **The god rays were drowning everything.** At weight 0.22 / exposure 1 the
  composite pinned *every pixel out to 20 degrees* at 255. The disc and its
  glare were being drawn correctly underneath a white sheet. The emitter was
  also 7 degrees wide, so it smeared into an even wash rather than into rays.
  Now 2.7 degrees at weight 0.07, which still lifts most of the frame under
  broken cloud (max +36 levels) while newly saturating a handful of pixels,
  and correctly contributes nothing under heavy overcast.

The sun is now two sprites: a **disc** and a **glare** halo around it. The glare is the important one - a true-size
disc is about eleven pixels tall on a 1080p viewport, and the reason you can
pick the sun out of a photograph is not its size but the glare an eye or a
lens wraps around it. Measured profile: +42 levels at one degree from the
disc, +22 at two, gone by eight.

Three things then make it read as the sun rather than a bright circle:

- **A hard, limb-darkened disc.** The photosphere is optically deep, so a
  sightline near the rim grazes through cooler, higher gas and comes back
  dimmer. The Eddington approximation gives I(mu)/I(0) = 1 - u(1 - mu) with
  mu = sqrt(1 - r²) at fractional disc radius r, and u about 0.5 in the visible
  band - larger toward the blue, which is why the rim is also slightly warmer
  than the core. The profile lives in the texture's colour channels, not its
  alpha: with normal blending alpha means *coverage*, so putting the darkening
  there would make the rim see-through and let the sky show through the sun.
- **The disc occludes the sky rather than adding to it.** It was an additive
  sprite, which cannot work: the atmospheric model draws its own saturated
  white spot exactly where the sun is, and anything added on top of an
  already-clipped pixel keeps no colour at all. That is why the setting sun
  rendered pure white however red its colour was set. It is now an opaque,
  tone-mapped source with a radiance rather than a tint.
- **Real atmospheric extinction sets its colour.** Kasten & Young (1989)
  relative air mass (which stays finite at the horizon where 1/sin(h)
  diverges), then per-channel Rayleigh-plus-aerosol optical depths. Nothing
  about the resulting progression is hand-picked - warm white overhead, gold by
  15°, deep orange at 4°, dark red on the horizon. The *hue* is applied in
  full; the **dimming is deliberately compressed**, because a real sun at the
  horizon is four orders of magnitude fainter than at noon, which at a fixed
  rendering exposure would simply delete it where a real eye would have
  adapted.

One more artifact had to go with it. The god-ray radial blur **peaks exactly on
the sun**, because every sample marches toward it - and that peak is not
physical: the photosphere is opaque, so no scattered light reaches the eye from
behind the disc. Adding it there clipped the disc to white at every elevation.
The composite is now suppressed inside the disc's own solid angle and feathered
out over about its diameter. Measured warmth (red minus blue at the disc's
centre) across a day: 2 overhead, 5 at 13°, 17 at 6°, **64 at 2.7°**, with the
disc the brightest thing in frame at every elevation and limb darkening
correct throughout.

`SUN_PEAK_RADIANCE` is a measured balance, not a taste call: at 2.7° elevation
a value of 9 leaves the disc 99 levels warmer than neutral but two levels
*darker* than the sky beside it - a dark orange hole rather than a sun, because
the sky model itself clips that close to the horizon. Doubling it puts the disc
above the sky but bleaches the warmth.

**Sizing and the brightness/colour trade.** The disc is drawn at 1.24°, about
2.3x the real sun. That is deliberate and it is the one place here that is
frankly not physical: at true size the disc is eleven pixels on a 1080p
viewport, and neither the limb darkening nor the extinction colour has enough
pixels to land on to be seen at all. Radiance is 26, which makes the disc
clearly the brightest thing in frame at every elevation - at the cost of
sunset colour, since a brighter disc sits further up the tone curve's
compressive shoulder. Measured warmth at 2.7° elevation fell from 64 at
radiance 16 to 40 at 26. That is the trade, and it is a real one: you can have
a brighter sun or a redder sunset, not both, until the sky model stops clipping
near the horizon.

**Ray strength.** God rays only form *shafts* where something breaks the mask;
on a clear sky the same pass produces nothing but a smooth wash around the sun,
and no strength setting distinguishes the two. Tuned so that at 63% cloud with
the sun 26° up they lift about 13% of the frame (130 of 1025 sampled pixels),
median +1 level, max +21 - present, not dominant. The near-sun wash on a clear
sky is unavoidable with this technique and is left in, because a degree or two
of blown white around the sun is what a real photograph shows anyway.

One bug worth recording, because it invalidated a whole round of measurements
before it was caught: both sprites were sitting at the **world origin**. They
are positioned in `animate()`, and in a hidden browser tab `requestAnimationFrame`
never fires, so every reading taken with a hand-driven `render()` had been
measuring the atmospheric sky's own bright patch and not the sun at all.

Clouds needed their own gain rather than `LIGHT_GAIN`: they already sat high
on the tone curve, so scaling them like the ground saturated them to flat
white (measured [249,250,250], 170 of 247 sampled pixels clipped). Retuned
against the sunlit tops: [215,222,223], spread 8, nothing clipped, shadowed
sides [145,153,155].

### Sub-hourly playback: resampling the weather feed

Open-Meteo's archive is hourly; the time slider moves in 15 minute steps. So
pressing play held each observation for four frames and then snapped - the sun
slid smoothly (it is computed per timestamp) while the clouds, the temperature
and the power sat still and jumped. `solar_twin/interpolation.py` fills the
gaps, server-side, so the physics stays in Python and the browser stays a thin
renderer. `GET /api/weather-day?date=&steps=4`.

**Irradiance is not interpolated directly.** Within one hour the sun moves
about 15 degrees, so a straight line between two hourly GHI values ignores the
largest thing happening in that hour. Instead the *clear-sky index* is
interpolated - the fraction of clear-sky irradiance that actually arrived,
which is a property of the atmosphere and changes slowly - and multiplied by
the clear-sky irradiance computed at the real sub-hourly timestamp. Fast
geometry comes from the Step 4 solar-position model; only slow transmission is
interpolated. There is a test that pins this: hold the atmosphere constant at
both ends and the interpolated irradiance traces the sun's own curve rather
than the chord across it.

Two things this turned up, both worth recording:

- **A single clearness cap was wrong.** Measured over ten days of Pavagada
  archive data (n=120 daylight hours), the clear-sky index runs: GHI median
  0.79 / max 1.82, DNI median 0.46 / max 0.94, but **DHI median 1.90, p95
  5.28, max 6.38**. Diffuse routinely runs several times its clear-sky value,
  because cloud converts beam *into* diffuse - that is the physics working.
  A shared 1.25 cap would have crushed three quarters of all real diffuse
  readings, some by a factor of five. The caps are now per component and set
  far above what real data does, so they bound a pathological hour without
  ever biting on a normal one.
- **Measured samples pass through untouched.** Reconstructing an observation
  from its own clear-sky index round-trips it through the cap and quietly
  rewrites real data. Only the gaps are filled.

Rainfall is an hourly *total*, not an instantaneous reading, so it is divided
across the sub-steps it covers rather than interpolated - the sum over any
interval is preserved, which is what the soiling model integrates.

Honest limits: **this adds resolution, not information.** Real irradiance
under broken cloud swings by hundreds of W/m² within seconds; nothing here can
recover variability the hourly feed never recorded, and sub-hourly values are
smooth by construction. Every interpolated sample is flagged `measured: false`
with `weather_source: "open_meteo_interpolated"`, carries its own warnings,
and is labelled in the UI's status line and metrics panel. Passing `steps=1`
returns the unmodified hourly series.

## Step 8: row-to-row shading and incidence-angle losses

```python
from solar_twin.array_geometry import apply_array_losses

plane = apply_array_losses(poa, position.zenith_deg, position.azimuth_deg, plant.row_geometry)
print(plane.shaded_row_fraction)          # 0.0 at midday, 0.35 near sunset
print(plane.effective_poa_global_w_m2)    # what the cells see, not what the plane receives
```

`transpose_to_poa` (Step 4) answers "how much light reaches a tilted plane
floating in the open?". A real array is a **field of parallel rows that shade
each other** at low sun, behind glass that **reflects light away at grazing
angles**. Both are pure geometry, both are always losses, and until this step
neither was modelled - so reported POA was optimistic exactly when the sun
was low. Worse, the 3D frontend already drew rows at a 6.8 m pitch while the
physics had no idea rows existed: **the renderer knew more about the plant's
geometry than the model did.**

Two published models, layered over an unmodified Step 4 result:

- **Passias & Källbäck (1984)** row shading for infinite parallel rows, the
  same geometry as `pvlib.shading.shaded_fraction_1d`. The shaded fraction of
  a row's slant height is `1 − P / (L·(cos β + sin β / tan ψ))`, where `P` is
  row pitch, `L` collector slant length, `β` tilt and `ψ` the **profile
  angle** - the sun's elevation projected into the plane perpendicular to the
  rows. Beam only.
- **ASHRAE incidence angle modifier**, `IAM = 1 − b₀·(1/cos θ − 1)` with
  `b₀ = 0.05` for uncoated glass - the same model as `pvlib.iam.ashrae`.

Row geometry now lives in the plant config, so the model and the renderer
share one source of truth: `module.length_m`/`width_m` (2.384 × 1.303 m, the
cited Trina datasheet, which also cross-checks to its 20.9% efficiency) and
`layout.modules_along_slope`/`row_pitch_m`. That gives **GCR 0.351** and a
shading onset at a **12.25° profile angle**.

**A finding worth stating plainly: at this site, row shading barely matters.**
A year-long scan (kept as a test) shows shading in only **2.1% of daylight
quarter-hours**, all within about half an hour of sunrise and sunset, because
14°N latitude plus a generous 6.8 m pitch means the sun is never both low and
due south. Tightening the pitch to 3.2 m would raise that to 33%. The model
earns its place by being able to say so, not by finding a big loss.

Honest limits, all recorded in `assumptions`: shading removes beam **in
proportion to shaded area**, whereas a real string is throttled by its worst
cell, so low-sun output here remains an upper bound; rows are infinite and
coplanar, so edge rows get no credit; **sky- and ground-diffuse are not
reduced for row-to-row masking**, so diffuse is overestimated by a few
percent; IAM is applied to beam only.

## Step 9: per-block asset state - soiling, degradation and faults

```python
from solar_twin.assets import BlockFault, nominal_state, with_faults, INVERTER_OFFLINE

state = nominal_state(plant, now, commissioned_utc=built, soiling_loss_fraction=0.035)
state = with_faults(state, {"BLK-007": BlockFault(INVERTER_OFFLINE, now, note="tripped")})
```

Every step up to here was a **pure function of the weather**, and
`simulate_dc` computed one number and repeated it twenty times. That is fine
for a design calculation and useless for a twin, because a twin's whole job
is to say that block 7 is not like the others. This step gives each block an
identity that persists: how dirty it is, how old it is, and what is wrong
with it.

**A fault is the single source of truth.** Availability, derating and excess
soiling are *derived properties* of the fault, never independently settable
fields, so a block's state cannot contradict itself. Four kinds:
`inverter_offline`, `inverter_derated`, `strings_disconnected`,
`localised_soiling`.

`solar_twin/soiling.py` implements **Kimber et al. (2014)** (the model behind
`pvlib.soiling.kimber`): soiling accrues linearly per dry day, rainfall above
a threshold inside a 24 h window resets it, and a grace period keeps the glass
clean afterwards. It is driven by **real Open-Meteo precipitation**, newly
added to `fetch_hourly_weather`. Run against Pavagada's actual rainfall it
produces a real seasonal cycle:

| Local date | Fleet soiling | Cleaning events in the 45-day lookback |
| --- | --- | --- |
| 2025-02-20 | 6.89% | 0 |
| 2025-05-15 | 4.07% | 2 |
| 2025-09-10 | 0.00% | 6 |
| 2025-12-20 | 0.30% | 1 |

The dry-season figures are pinned at the lookback limit, which the API says
out loud: `no_rain_within_the_45_day_lookback_so_soiling_is_a_lower_bound`.

Degradation is the **module warranty curve** (1% first year, then 0.4%/yr,
reaching 89.4% retention at 25 years) - a guaranteed floor, not a measured
rate. Faults are **declared, not detected**; nothing here infers a fault from
data, and there is no failure or repair model.

## Step 10: per-block simulation and the loss waterfall

```python
from solar_twin.fleet import simulate_fleet

result = simulate_fleet(plant, conditions, plane, state, timestamp)
for step, mw in result.loss_waterfall:
    print(f"{step:<32} {mw:>8.3f}")
```

`simulate_fleet` runs each block under its own state and sums the plant from
the parts, reusing every earlier step unmodified: POA (4) → shading and
reflection (8) → per-block DC (2) → per-block inverter (3) → grid cascade
(6). `grid.py` gained `apply_grid_losses`, which takes a **per-block list**
of inverter outputs rather than one plant total - which is what lets an
offline block contribute no power while its transformer, still energised on
the collector network, keeps costing the plant its no-load loss.

It produces the artifact plant engineers actually read: an itemised **loss
waterfall from nameplate irradiance down to net export that closes exactly**
(asserted to 1e-9 in the tests). Every megawatt that fails to reach the grid
is attributed to a named cause rather than vanishing into an unexplained
derate factor:

```
nameplate_dc_at_incident_poa       121.704
row_shading                         -0.000     glass_reflection      -0.108
cell_temperature                   -15.248     soiling               -3.510
module_degradation                  -2.175     string_availability   -1.262
inverter_outage                     -5.049     inverter_clipping     -1.503
inverter_conversion                 -1.193     block_transformers    -0.664
collector_cables                    -0.910     grid_transformer      -0.320
hv_cables                           -0.180     auxiliary_load        -0.163
curtailment                         -0.000  →  net export            89.422
```

Getting that to close surfaced a real modelling question: an offline
inverter does not make its array's DC *disappear*, it **strands** it. So a
block reports both `array_dc_mw` (what it could make) and `available_dc_mw`
(what reaches a working inverter), with the difference charged to a separate
`inverter_outage` line.

Beyond the limits earlier steps already declare: a block is the finest
resolution, every block sees identical irradiance and air temperature (no
cloud shadow crossing the site), and cell temperature uses pre-shading POA so
a shaded block is not credited with running cooler.

## Step 11: energy, specific yield and Performance Ratio

```python
from solar_twin.energy import summarise_fleet_day

totals = summarise_fleet_day(plant, day_of_results)
print(totals.performance_ratio)          # 0.7876
print(totals.specific_yield_kwh_per_kwp) # 5.95 kWh/kWp
```

`timeseries.py` deliberately stopped short of this ("turning a series of
instantaneous power snapshots into energy is a distinct, still-future step").
This is that step. Nobody runs a plant on instantaneous megawatts; the
numbers that matter are MWh, kWh per kWp, and above all **Performance Ratio**,
which divides out the weather and leaves the plant.

Definitions follow **IEC 61724-1**: `PR = E_out / (H_poa · P_dc0 / G_stc)`
with `G_stc = 1 kW/m²`. `H_poa` is irradiation in the plane of array
**before shading** - what a reference pyranometer on the array plane would
read - which is deliberate, because row shading is a plant loss and PR must
be charged for it.

Honest limits: energy is integrated **by trapezoid**, i.e. power is assumed
linear between samples, which is good near midday and poor at sunrise and
sunset; PR is **not temperature corrected**, so summer and winter PR are not
comparable; and the input is simulated power, so these are modelled figures,
not revenue-meter data.

## Step 12: model vs. measurement - the residual

```python
from solar_twin.measurement import synthesize_measurement, compare_to_model

report = compare_to_model(synthesize_measurement(plant_with_faults), twins_model)
print(report.flagged_block_ids)   # ('BLK-007', 'BLK-012')
```

This is the step that makes the project a **twin** rather than a simulator. A
simulator answers "what should this plant produce?". A twin answers "what is
this plant producing that it should not be?" - and the only way to ask that
is to put a modelled block next to a measured one.

There is no physical plant here, so measurement is **synthesised**: run the
fleet under a *true* state containing faults, add sensor noise, and hand the
result to a twin that believes the *nominal healthy* state. The synthetic
origin is stamped on every result and never presented as real. Noise is
seeded via `zlib.crc32` rather than `hash()`, because Python randomises string
hashing per process and the same hour must read the same on every restart.

**Detection is by peer comparison, not absolute threshold.** Each block's
measured/modelled ratio is compared against the **fleet median** of that
ratio. This matters: if the irradiance input is 12% low, every block reads
12% low, and an absolute threshold would flag all twenty. Dividing by the
median cancels error common to the whole plant and leaves only what is
block-specific - the same trick real PV monitoring uses, and the reason this
works at all on top of a reanalysis weather feed that is itself approximate.
It recovers injected severities closely: a 25% string outage reads −24.8%, an
8% soiling patch reads −8.2%.

**Clipping hides faults, and the report says so.** At a 1.3 DC/AC ratio every
healthy block is pinned at inverter nameplate around midday, so a block
losing less than the clipped headroom reports full output and is invisible
until the sun drops:

| Local hour | Clipping? | BLK-019 (8% soiling) | BLK-012 (25% strings) |
| --- | --- | --- | --- |
| 09:30 | no | −8.79% · flagged | −24.48% · flagged |
| 12:00 | **yes** | +1.34% · **missed** | −19.56% · flagged |
| 16:00 | no | −7.58% · flagged | −24.66% · flagged |

That is a real monitoring phenomenon reproduced, not a tuning failure, and no
amount of threshold adjustment fixes it.

Other limits: this flags anomalies, it does not **diagnose** them; peer
comparison is blind by construction to a fault affecting every block equally
(fleet-wide soiling moves the median with everything else); it needs daylight
and at least three producing blocks; and flags are instantaneous, with no
persistence ranking - that is what the history layer is for.

`solar_twin/history.py` adds a **stdlib `sqlite3`** store (no new dependency)
for daily energy, per-block daily totals and a fault log, so the twin can say
*"BLK-007 has been flagged nine days running"* rather than only *"BLK-007 is
low right now"*. It records what the model said; it is not an authority the
physics reads back from, and it has no migrations.

## Step 13: the frontend as an instrument

```sh
python3 -m solar_twin.server --port 8000
# then open http://127.0.0.1:8000
```

Step 7's frontend was a **diorama**: pretty, and with nothing to interrogate,
because the model behind it had 20 identical blocks and no memory. With Steps
8-12 behind it, the same scene becomes an instrument.

- A **plant map** laid out five across and four down - the same order the 3D
  scene builds blocks in, so a cell's position on screen matches the block's
  position on the ground. Colour is the **residual**, not raw output, so a
  cloudy hour does not turn the whole site amber.
- **Click any block**, on the map or in the 3D scene, for its own numbers:
  measured vs expected, its gap against the fleet median, its soiling, age
  and clipping.
- **Inject a fault** from the browser and watch the plant diverge from the
  twin. The twin keeps modelling the healthy plant; the gap is the product.
- A **loss waterfall** and a **day power curve** with the healthy-plant
  counterfactual drawn over it, plus PR, specific yield and capacity factor.
- Status **beacons** over each block in the 3D scene, so a dark block is
  findable from 700 m up.

New routes, alongside Step 7's: `GET /api/day?date=&steps=&record=`,
`GET /api/history`, `GET /api/scenario` and `POST /api/scenario/faults`.
The server holds one mutable thing, a `Scenario` - the twin's declared belief
about the physical plant - and runs every simulation **twice** against it:
once under the healthy nominal state, once under the declared state. Model
warnings and assumptions from every layer, including where the soiling figure
came from, surface in the data drawer rather than being dropped at the API
boundary.

## Licence

Copyright © 2026 Likith Muninarakala. **All rights reserved.** See
[LICENSE](LICENSE).

This code is published for evaluation and reference. It is *not* open source:
no permission is granted to use, copy, modify or redistribute it. Reading it
and running it locally to evaluate it are fine; anything else needs a written
licence. Commercial licences are available — contact
<likithmuni.2004@gmail.com>.

The scientific models implemented here are published, public science and are
not claimed by that copyright; only this implementation of them is. three.js
and Google Fonts load from third-party CDNs at runtime under their own
licences.
