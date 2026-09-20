"""Resample an hourly weather series onto a finer time step.

Open-Meteo's archive is hourly. The frontend's time slider moves in 15 minute
steps, so playing back a day held each hourly observation for four steps and
then jumped - the sun slid smoothly (it is computed per timestamp) while the
clouds, the temperature and the power sat still and then snapped. This module
fills in the gaps.

**Irradiance is not interpolated directly.** Within one hour the sun moves
about 15 degrees, so a straight line between two hourly GHI values ignores the
single largest thing happening in that hour. Instead this interpolates the
*clear-sky index* - the fraction of clear-sky irradiance that actually arrived,
which is a property of the atmosphere and changes slowly - and then multiplies
it by the clear-sky irradiance computed at the real sub-hourly timestamp. The
fast-moving geometry comes from the Step 4 solar-position model; only the
slow-moving transmission is interpolated. This is the standard way to
downscale irradiance in time, and it is much better behaved near sunrise and
sunset, where linear interpolation of GHI is badly wrong.

Everything else is interpolated linearly, except rainfall, which is an hourly
*total* rather than an instantaneous reading and so is divided across the
sub-steps it covers, preserving the sum over any interval.

Honest limits, carried on the result:

* This adds resolution, not information. Real irradiance under broken cloud
  fluctuates by hundreds of W/m2 within seconds; nothing here can recover
  variability the hourly feed never recorded. Sub-hourly values are smooth by
  construction and will understate real short-term swings.
* The clear-sky reference is pvlib's Ineichen-Perez model (`clear_sky.py`),
  so the index is only as good as that reference and its Linke turbidity
  climatology. The diffuse reference is by definition a *clear-sky* diffuse,
  so the diffuse index is routinely well above 1 under cloud - that is the
  model working, not an error.
* Measured samples pass through untouched. Only the gaps between them are
  filled, so nothing here ever rewrites a real observation.
* Interpolated samples are marked as such, and never presented as measured.
"""

from dataclasses import replace
from datetime import timedelta

from .clear_sky import clear_sky_irradiance
from .location import Location
from .solar_position import solar_position
from .validation import number_in_range
from .weather import WeatherObservation

# Below this the clear-sky reference is too small to divide by meaningfully;
# the sun is at or under the horizon and the product is zero either way.
_MINIMUM_REFERENCE_W_M2 = 1.0

# The index is bounded only so that one pathological hour cannot propagate into
# its neighbours; these caps are deliberately far above what real data does, so
# they never bite on a normal day. They are per component because the three
# components behave completely differently against a clear-sky reference.
# Measured over ten days of Pavagada archive data (n=120 daylight hours):
#
#   GHI  median 0.79, p95 1.40, max 1.82   - cloud enhancement pushes it past 1
#   DNI  median 0.46, p95 0.88, max 0.94   - beam is only ever attenuated
#   DHI  median 1.90, p95 5.28, max 6.38   - cloud turns beam INTO diffuse
#
# That last row is why a single shared cap was wrong: at 1.25 it would have
# crushed three quarters of all real diffuse readings, some by a factor of five.
_MAXIMUM_CLEARNESS = {"ghi": 3.0, "dni": 1.5, "dhi": 12.0}


def _clear_sky_reference(location: Location, timestamp) -> tuple[float, float, float]:
    """Clear-sky GHI/DNI/DHI at one instant, as the index's denominator."""
    position = solar_position(location, timestamp)
    reference = clear_sky_irradiance(location, timestamp, position.zenith_deg)
    return reference.ghi_w_m2, reference.dni_w_m2, reference.dhi_w_m2


def _clearness(measured: float, reference: float, component: str) -> float | None:
    """Ratio of measured to clear-sky for one component, or None when undefined.

    Above 1 is normal and physical: cloud converts beam to diffuse, so DHI
    routinely runs several times its clear-sky value.
    """
    if reference < _MINIMUM_REFERENCE_W_M2:
        return None
    return min(_MAXIMUM_CLEARNESS[component], max(0.0, measured / reference))


def _blend(a: float | None, b: float | None, t: float) -> float:
    """Linear blend that tolerates an undefined end - at night one side has no index."""
    if a is None and b is None:
        return 0.0
    if a is None:
        return b
    if b is None:
        return a
    return a + (b - a) * t


def interpolate_weather(
    location: Location,
    observations: list[WeatherObservation],
    steps_per_hour: int = 4,
) -> list[WeatherObservation]:
    """Resample an hourly series to `steps_per_hour` samples per hour.

    The returned series starts at the first observation and ends at the last,
    and reproduces the originals exactly at their own timestamps.
    """
    if type(steps_per_hour) is not int:
        raise ValueError("steps_per_hour must be an integer")
    number_in_range("steps_per_hour", steps_per_hour, 1, 60)
    if len(observations) < 2:
        raise ValueError("At least two observations are needed to interpolate between them")
    for index in range(1, len(observations)):
        if observations[index].timestamp_utc <= observations[index - 1].timestamp_utc:
            raise ValueError("Observations must be in strictly increasing time order")
    if steps_per_hour == 1:
        return list(observations)

    # Clear-sky index per source observation, per component.
    indices = []
    for observation in observations:
        ghi_cs, dni_cs, dhi_cs = _clear_sky_reference(location, observation.timestamp_utc)
        indices.append((
            _clearness(observation.ghi_w_m2, ghi_cs, "ghi"),
            _clearness(observation.dni_w_m2, dni_cs, "dni"),
            _clearness(observation.dhi_w_m2, dhi_cs, "dhi"),
        ))

    resampled: list[WeatherObservation] = []
    for index in range(len(observations) - 1):
        start, end = observations[index], observations[index + 1]
        span = (end.timestamp_utc - start.timestamp_utc).total_seconds()
        # Rain is a total for the interval, so spreading it evenly keeps the
        # sum intact instead of smearing a downpour into its neighbours.
        rain_per_step = start.precipitation_mm / steps_per_hour
        for step in range(steps_per_hour):
            t = step / steps_per_hour
            if step == 0:
                # A measured sample passes through untouched. Reconstructing it
                # from its own clear-sky index would round-trip through the
                # clamp and quietly rewrite real data.
                resampled.append(replace(start, precipitation_mm=rain_per_step))
                continue
            timestamp = start.timestamp_utc + timedelta(seconds=span * t)
            ghi_cs, dni_cs, dhi_cs = _clear_sky_reference(location, timestamp)
            k_ghi = _blend(indices[index][0], indices[index + 1][0], t)
            k_dni = _blend(indices[index][1], indices[index + 1][1], t)
            k_dhi = _blend(indices[index][2], indices[index + 1][2], t)
            resampled.append(WeatherObservation(
                timestamp_utc=timestamp,
                ghi_w_m2=min(1500.0, k_ghi * ghi_cs),
                dni_w_m2=min(1500.0, k_dni * dni_cs),
                dhi_w_m2=min(1500.0, k_dhi * dhi_cs),
                air_temperature_c=start.air_temperature_c
                    + (end.air_temperature_c - start.air_temperature_c) * t,
                wind_speed_10m_m_s=start.wind_speed_10m_m_s
                    + (end.wind_speed_10m_m_s - start.wind_speed_10m_m_s) * t,
                cloud_cover_percent=start.cloud_cover_percent
                    + (end.cloud_cover_percent - start.cloud_cover_percent) * t,
                precipitation_mm=rain_per_step,
            ))
    resampled.append(observations[-1])   # frozen, so sharing is safe
    return resampled


def interpolation_warnings(steps_per_hour: int) -> tuple[str, ...]:
    """What a consumer of the resampled series has to be told."""
    if steps_per_hour <= 1:
        return ()
    return (
        "sub_hourly_values_are_interpolated_from_hourly_observations_not_measured",
        "irradiance_downscaled_by_interpolating_clear_sky_index_not_by_interpolating_irradiance",
        "interpolation_adds_resolution_not_information_real_broken_cloud_variability_is_not_recovered",
    )
