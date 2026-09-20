"""Step 11: turn a day of instantaneous power into energy, yield and PR.

Every step up to here reported power at one instant. Nobody runs a solar
plant on instantaneous megawatts: the numbers that matter are MWh delivered,
kWh per kWp installed, and above all **Performance Ratio** - the fraction of
the energy that the measured irradiance and the installed capacity said was
available that actually reached the meter. PR is the single number the solar
industry uses to say whether a plant is doing its job, because it divides out
the weather and leaves the plant.

`timeseries.py` deliberately stopped short of this ("turning a series of
instantaneous power snapshots into energy is a distinct, still-future step").
This is that step.

Definitions follow IEC 61724-1, the PV system performance standard:

    PR = E_out / (H_poa * P_dc0 / G_stc),   G_stc = 1 kW/m^2

`H_poa` is irradiation in the **plane of array before shading** - what a
reference pyranometer mounted on the array plane would read. That is
deliberate: row shading is a plant loss, so PR must be charged for it.

Honest limits:

* Energy is integrated by trapezoid between samples, i.e. power is assumed to
  vary linearly between them. On hourly data this is good near midday and
  poor at sunrise and sunset, where real power is strongly curved. Sub-hourly
  input reduces the error; nothing here corrects for it.
* The input series is simulated power, so these are *modeled* energy figures.
  A real PR is computed from a revenue meter and a calibrated pyranometer.
* No temperature-corrected PR. The plain PR defined above falls in hot weather
  by construction, so summer and winter PR are not directly comparable.
* No availability-weighted or contractual PR variants.
"""

from dataclasses import dataclass
from datetime import datetime

from .plant import Plant
from .solar_position import _require_utc
from .validation import number_in_range

STANDARD_IRRADIANCE_W_M2 = 1000.0


def integrate_trapezoid(timestamps: list[datetime], values: list[float]) -> float:
    """Integrate a sampled series over time. Units in -> the same units * hours."""
    if len(timestamps) != len(values):
        raise ValueError("timestamps and values must be the same length")
    if len(timestamps) < 2:
        raise ValueError("At least two samples are needed to integrate over time")
    for timestamp in timestamps:
        _require_utc(timestamp)
    total = 0.0
    for index in range(1, len(timestamps)):
        hours = (timestamps[index] - timestamps[index - 1]).total_seconds() / 3600
        if hours <= 0:
            raise ValueError("timestamps must be strictly increasing")
        total += hours * (values[index] + values[index - 1]) / 2
    return total


@dataclass(frozen=True)
class EnergyTotals:
    """A day (or any window) summed up, with the industry's yield metrics."""

    start_utc: str
    end_utc: str
    span_hours: float
    samples: int
    mean_sample_interval_minutes: float
    poa_irradiation_kwh_m2: float
    effective_poa_irradiation_kwh_m2: float
    dc_energy_mwh: float
    inverter_ac_energy_mwh: float
    export_energy_mwh: float
    curtailed_energy_mwh: float
    specific_yield_kwh_per_kwp: float
    performance_ratio: float | None
    performance_ratio_at_inverter: float | None
    capacity_factor: float | None
    peak_export_mw: float
    warnings: tuple[str, ...]
    scope: str = "Modeled energy integrated from simulated power; not revenue-meter data"
    assumptions: tuple[str, ...] = (
        "power_assumed_to_vary_linearly_between_samples_trapezoid_integration",
        "performance_ratio_uses_unshaded_plane_of_array_irradiation_per_iec_61724",
        "performance_ratio_is_not_temperature_corrected_so_seasons_are_not_comparable",
        "energy_is_modeled_not_measured_no_revenue_meter_or_calibrated_pyranometer",
    )


def summarise_energy(
    plant: Plant,
    timestamps: list[datetime],
    incident_poa_w_m2: list[float],
    effective_poa_w_m2: list[float],
    available_dc_mw: list[float],
    inverter_ac_mw: list[float],
    net_export_mw: list[float],
    curtailment_mw: list[float],
) -> EnergyTotals:
    """Integrate one window of simulated power into energy and yield metrics."""
    series = {
        "incident_poa_w_m2": incident_poa_w_m2,
        "effective_poa_w_m2": effective_poa_w_m2,
        "available_dc_mw": available_dc_mw,
        "inverter_ac_mw": inverter_ac_mw,
        "net_export_mw": net_export_mw,
        "curtailment_mw": curtailment_mw,
    }
    for name, values in series.items():
        if len(values) != len(timestamps):
            raise ValueError(f"{name} must have one value per timestamp")
        for value in values:
            number_in_range(name, value, 0, 1e6)

    span_hours = (timestamps[-1] - timestamps[0]).total_seconds() / 3600 if len(timestamps) > 1 else 0.0
    poa_kwh_m2 = integrate_trapezoid(timestamps, incident_poa_w_m2) / 1000
    effective_kwh_m2 = integrate_trapezoid(timestamps, effective_poa_w_m2) / 1000
    dc_mwh = integrate_trapezoid(timestamps, available_dc_mw)
    ac_mwh = integrate_trapezoid(timestamps, inverter_ac_mw)
    export_mwh = integrate_trapezoid(timestamps, net_export_mw)
    curtailed_mwh = integrate_trapezoid(timestamps, curtailment_mw)

    dc_capacity_kwp = plant.dc_capacity_mwp * 1000
    reference_kwh = poa_kwh_m2 * dc_capacity_kwp / (STANDARD_IRRADIANCE_W_M2 / 1000)

    warnings = []
    performance_ratio = performance_ratio_at_inverter = None
    if reference_kwh > 0:
        performance_ratio = export_mwh * 1000 / reference_kwh
        performance_ratio_at_inverter = ac_mwh * 1000 / reference_kwh
        if performance_ratio > 1:
            warnings.append("performance_ratio_above_one_check_the_irradiance_series")
    else:
        warnings.append("no_plane_of_array_irradiation_in_this_window_performance_ratio_undefined")

    interval = span_hours * 60 / (len(timestamps) - 1) if len(timestamps) > 1 else 0.0
    if interval > 30:
        warnings.append("sample_interval_over_thirty_minutes_trapezoid_energy_error_is_largest_at_sunrise_and_sunset")
    if span_hours < 23 or span_hours > 25:
        warnings.append("window_is_not_a_whole_day_daily_yield_metrics_are_partial")

    capacity_factor = None
    if span_hours > 0:
        capacity_factor = export_mwh / (plant.ac_capacity_mw * span_hours)

    return EnergyTotals(
        start_utc=timestamps[0].isoformat(),
        end_utc=timestamps[-1].isoformat(),
        span_hours=span_hours,
        samples=len(timestamps),
        mean_sample_interval_minutes=interval,
        poa_irradiation_kwh_m2=poa_kwh_m2,
        effective_poa_irradiation_kwh_m2=effective_kwh_m2,
        dc_energy_mwh=dc_mwh,
        inverter_ac_energy_mwh=ac_mwh,
        export_energy_mwh=export_mwh,
        curtailed_energy_mwh=curtailed_mwh,
        specific_yield_kwh_per_kwp=export_mwh * 1000 / dc_capacity_kwp,
        performance_ratio=performance_ratio,
        performance_ratio_at_inverter=performance_ratio_at_inverter,
        capacity_factor=capacity_factor,
        peak_export_mw=max(net_export_mw) if net_export_mw else 0.0,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def summarise_fleet_day(plant: Plant, results: list) -> EnergyTotals:
    """Convenience wrapper over a list of `fleet.FleetResult`."""
    if len(results) < 2:
        raise ValueError("At least two fleet results are needed to integrate a day")
    timestamps = [datetime.fromisoformat(result.timestamp_utc) for result in results]
    return summarise_energy(
        plant, timestamps,
        [r.array_plane.incident.poa_global_w_m2 for r in results],
        [r.array_plane.effective_poa_global_w_m2 for r in results],
        [r.available_dc_mw for r in results],
        [r.plant_inverter_ac_mw for r in results],
        [r.net_export_mw for r in results],
        [r.curtailment_mw for r in results],
    )
