"""Solar zenith/azimuth via the PSA algorithm.

Blanco-Muriel, Alarcon-Padilla, Lopez-Moratalla, Lara-Coira, "Computing the
solar vector," Solar Energy 70(5), 2001, pp. 431-441. A compact closed-form
algorithm (~0.01 deg accuracy) used in real solar-tracking systems; it has no
dependency on ephemeris tables, matching this project's practice of
implementing published equations directly (see the Sandia temperature model
and PVWatts v5 equation in dc.py) rather than depending on a library.

The original paper documents accuracy over 1999-2015; a 2020 revision by the
same group extends the valid window. Dates outside 1999-2015 are still
computed (the underlying series does not have a hard cutoff) but are flagged
in `warnings` rather than silently trusted, matching the extrapolation
warnings already used for cell temperature.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import math

from .location import Location

_EARTH_MEAN_RADIUS_KM = 6371.01
_ASTRONOMICAL_UNIT_KM = 149597890.0


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


def solar_position(location: Location, timestamp_utc: datetime) -> SolarPosition:
    """Topocentric-ish zenith/azimuth (parallax-corrected zenith only).

    azimuth_deg is measured clockwise from north (0=N, 90=E, 180=S, 270=W).
    zenith_deg >= 90 means the sun is below the horizon.
    """
    _require_utc(timestamp_utc)

    decimal_hours = (timestamp_utc.hour + timestamp_utc.minute / 60
                     + (timestamp_utc.second + timestamp_utc.microsecond / 1e6) / 3600)
    year = timestamp_utc.year
    # UTC noon on 2000-01-01 is JD 2451545.0. Datetime arithmetic avoids
    # translating C integer division into Python floor division incorrectly.
    epoch = datetime(2000, 1, 1, 12, tzinfo=timezone.utc)
    julian_date = 2451545.0 + (timestamp_utc - epoch).total_seconds() / 86400
    elapsed_julian_days = julian_date - 2451545.0

    omega = 2.1429 - 0.0010394594 * elapsed_julian_days
    mean_longitude = 4.8950630 + 0.017202791698 * elapsed_julian_days
    mean_anomaly = 6.2400600 + 0.0172019699 * elapsed_julian_days
    ecliptic_longitude = (mean_longitude + 0.03341607 * math.sin(mean_anomaly)
                          + 0.00034894 * math.sin(2 * mean_anomaly) - 0.0001134
                          - 0.0000203 * math.sin(omega))
    ecliptic_obliquity = 0.4090928 - 6.2140e-9 * elapsed_julian_days + 0.0000396 * math.cos(omega)

    sin_ecliptic_longitude = math.sin(ecliptic_longitude)
    y = math.cos(ecliptic_obliquity) * sin_ecliptic_longitude
    x = math.cos(ecliptic_longitude)
    right_ascension = math.atan2(y, x)
    if right_ascension < 0:
        right_ascension += 2 * math.pi
    declination = math.asin(math.sin(ecliptic_obliquity) * sin_ecliptic_longitude)

    greenwich_sidereal_time = 6.6974243242 + 0.0657098283 * elapsed_julian_days + decimal_hours
    local_sidereal_time = math.radians(greenwich_sidereal_time * 15 + location.longitude_deg)
    hour_angle = local_sidereal_time - right_ascension
    latitude_rad = math.radians(location.latitude_deg)
    cos_latitude, sin_latitude = math.cos(latitude_rad), math.sin(latitude_rad)
    cos_hour_angle = math.cos(hour_angle)

    cos_zenith = (cos_latitude * cos_hour_angle * math.cos(declination)
                  + math.sin(declination) * sin_latitude)
    zenith_rad = math.acos(max(-1.0, min(1.0, cos_zenith)))
    azimuth_rad = math.atan2(-math.sin(hour_angle),
                              math.tan(declination) * cos_latitude - sin_latitude * cos_hour_angle)
    if azimuth_rad < 0:
        azimuth_rad += 2 * math.pi

    parallax_rad = (_EARTH_MEAN_RADIUS_KM / _ASTRONOMICAL_UNIT_KM) * math.sin(zenith_rad)
    zenith_deg = math.degrees(zenith_rad + parallax_rad)
    azimuth_deg = math.degrees(azimuth_rad)

    warnings = []
    if not 1999 <= year <= 2015:
        warnings.append("date_outside_psa_validity_window")
    return SolarPosition(zenith_deg=zenith_deg, azimuth_deg=azimuth_deg, warnings=tuple(warnings))
