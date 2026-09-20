"""Solar zenith/azimuth via NREL's SPA, through pvlib.

Reda & Andreas, "Solar Position Algorithm for Solar Radiation Applications",
NREL/TP-560-34302. SPA is accurate to about 0.0003 degrees over the years
-2000 to 6000, and is the reference implementation the rest of the industry
measures against.

This replaced a hand-written PSA implementation (Blanco-Muriel et al. 2001,
~0.01 degrees, documented for 1999-2015). PSA was accurate enough in practice
and cost nothing to depend on, but it was a re-implementation of published
equations sitting next to a library that already had a better one, tested by
far more people. Two things improve concretely: accuracy by roughly two orders
of magnitude, and the validity window - the old code had to warn that dates
outside 1999-2015 were extrapolated, and that warning is simply gone.

The scalar signature is kept deliberately. pvlib is vectorised and wants a
DatetimeIndex; the rest of this project is built around one timestamp at a
time, and changing that would ripple through every caller. See
`solar_position_series` for the vectorised path where it matters.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
import pvlib

from .location import Location


@dataclass(frozen=True)
class SolarPosition:
    zenith_deg: float
    azimuth_deg: float
    warnings: tuple[str, ...]


def _require_utc(timestamp_utc: datetime) -> None:
    if not isinstance(timestamp_utc, datetime):
        raise ValueError("timestamp_utc must be a datetime")
    if timestamp_utc.tzinfo is None or timestamp_utc.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("timestamp_utc must be a timezone-aware UTC datetime")


def solar_position_series(location: Location, timestamps_utc: list[datetime]) -> pd.DataFrame:
    """Vectorised SPA for many timestamps at once.

    Returns pvlib's frame directly: `apparent_zenith`, `zenith`, `azimuth`,
    `elevation`, `apparent_elevation`, `equation_of_time`.
    """
    if not timestamps_utc:
        raise ValueError("At least one timestamp is required")
    for timestamp in timestamps_utc:
        _require_utc(timestamp)
    index = pd.DatetimeIndex(timestamps_utc)
    # Refraction depends on how much atmosphere the ray crosses, so station
    # pressure matters near the horizon. Deriving it from the site's elevation
    # rather than leaving pvlib's sea-level default is free and correct: at
    # Pavagada's 700 m it is about 8% less refraction. Verified against
    # NREL/TP-560-34302 A.5, which matches to 0.01 arcsec when the paper's own
    # pressure and temperature are supplied.
    return pvlib.solarposition.spa_python(
        index, latitude=location.latitude_deg, longitude=location.longitude_deg,
        altitude=location.elevation_m,
        pressure=pvlib.atmosphere.alt2pres(location.elevation_m),
    )


def solar_position(location: Location, timestamp_utc: datetime) -> SolarPosition:
    """Zenith/azimuth at one instant.

    azimuth_deg is measured clockwise from north (0=N, 90=E, 180=S, 270=W).
    zenith_deg >= 90 means the sun is below the horizon.

    The *apparent* (refracted) zenith is reported, which is what a pyranometer
    on the array actually sees and what pvlib's transposition models expect.
    Atmospheric refraction lifts the sun by roughly half a degree at the
    horizon - the old PSA implementation ignored it entirely.
    """
    _require_utc(timestamp_utc)
    frame = solar_position_series(location, [timestamp_utc])
    row = frame.iloc[0]

    warnings = []
    if row["apparent_zenith"] >= 90:
        warnings.append("sun_below_horizon")
    return SolarPosition(
        zenith_deg=float(row["apparent_zenith"]),
        azimuth_deg=float(row["azimuth"]),
        warnings=tuple(warnings),
    )
