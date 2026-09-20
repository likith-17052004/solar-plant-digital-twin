"""Clear-sky irradiance, via pvlib's Ineichen-Perez model.

This is the reference the rest of the project measures against: the synthetic
demo path uses it directly, and `interpolation.py` divides measured irradiance
by it to get a clear-sky index.

Ineichen & Perez (2002), as implemented by `pvlib.clearsky.ineichen`. It takes
a Linke turbidity - how hazy the atmosphere is - and returns a physically
consistent GHI, DNI and DHI *together*.

That last word is the point of this rewrite. The previous version paired
Haurwitz (1945), which gives GHI only, with a made-up 15% diffuse fraction,
and its own docstring conceded the split was "explicitly not a validated
decomposition model". A fixed fraction is wrong in a specific, knowable way:
the real diffuse share climbs steeply as the sun drops, because the beam
crosses more atmosphere. Ineichen reproduces that - at Pavagada it runs about
0.18 with the sun high and 0.40 near the horizon, against the flat 0.15 that
was there before.

Linke turbidity comes from pvlib's bundled monthly climatology rather than a
guess. Pavagada reads about 4.7, consistent with a hazy semi-arid site.
"""

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

import pandas as pd
import pvlib

from .location import Location
from .validation import number_in_range

# Clear-sky demo has no cloud cover; weather mode supplies its own value.
SYNTHETIC_CLOUD_COVER_FRACTION = 0.0


@dataclass(frozen=True)
class ClearSkyIrradiance:
    ghi_w_m2: float
    dni_w_m2: float
    dhi_w_m2: float


@lru_cache(maxsize=512)
def _linke_turbidity(latitude: float, longitude: float, year: int, month: int) -> float:
    """Monthly Linke turbidity for a site, from pvlib's bundled climatology.

    Cached because the lookup reads a packaged data file, and the value only
    varies by site and calendar month.
    """
    index = pd.DatetimeIndex([f"{year:04d}-{month:02d}-15"], tz="UTC")
    return float(pvlib.clearsky.lookup_linke_turbidity(index, latitude, longitude).iloc[0])


def clear_sky_irradiance(
    location: Location, timestamp_utc: datetime, apparent_zenith_deg: float,
) -> ClearSkyIrradiance:
    """Clear-sky GHI/DNI/DHI at one instant, as a consistent triple."""
    number_in_range("apparent_zenith_deg", apparent_zenith_deg, 0, 180)
    if apparent_zenith_deg >= 90:
        return ClearSkyIrradiance(0.0, 0.0, 0.0)

    pressure = pvlib.atmosphere.alt2pres(location.elevation_m)
    relative = pvlib.atmosphere.get_relative_airmass(apparent_zenith_deg)
    absolute = pvlib.atmosphere.get_absolute_airmass(relative, pressure)
    turbidity = _linke_turbidity(location.latitude_deg, location.longitude_deg,
                                 timestamp_utc.year, timestamp_utc.month)
    frame = pvlib.clearsky.ineichen(apparent_zenith_deg, absolute, turbidity,
                                    altitude=location.elevation_m)
    return ClearSkyIrradiance(
        ghi_w_m2=float(frame["ghi"]),
        dni_w_m2=float(frame["dni"]),
        dhi_w_m2=float(frame["dhi"]),
    )


def synthesize_air_temperature_c(
    local_hour_decimal: float, mean_c: float = 28.0, amplitude_c: float = 8.0,
    minimum_local_hour: float = 6.0,
) -> float:
    """Simple diurnal sinusoid keyed to local hour.

    Demo support, not a forecast, and the one thing here pvlib has no opinion
    about: representative of Pavagada, not measured for any particular day.
    """
    number_in_range("local_hour_decimal", local_hour_decimal, 0, 24)
    import math
    phase = 2 * math.pi * (local_hour_decimal - minimum_local_hour) / 24
    return mean_c - amplitude_c * math.cos(phase)
