"""Historical hourly weather fetch: DNI/DHI/GHI, air temperature, wind, rain.

Uses the free, keyless Open-Meteo Historical Weather (Archive) API via
stdlib `urllib`, keeping the project dependency-free. This is reanalysis
model data, not ground-truth pyranometer measurement, and the archive
typically lags real time by several days; treat it as representative
weather, not a certified resource assessment.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import urllib.error
import urllib.parse
import urllib.request

from .location import Location
from .validation import number_in_range
from .solar_position import _require_utc

_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
_HOURLY_FIELDS = (
    "direct_normal_irradiance",
    "diffuse_radiation",
    "shortwave_radiation",
    "temperature_2m",
    "wind_speed_10m",
    "cloud_cover",
    "precipitation",
)


@dataclass(frozen=True)
class WeatherObservation:
    timestamp_utc: datetime
    dni_w_m2: float
    dhi_w_m2: float
    ghi_w_m2: float
    air_temperature_c: float
    wind_speed_10m_m_s: float
    cloud_cover_percent: float
    precipitation_mm: float

    def __post_init__(self):
        _require_utc(self.timestamp_utc)
        for field in ("dni_w_m2", "dhi_w_m2", "ghi_w_m2"):
            number_in_range(field, getattr(self, field), 0, 1500)
        number_in_range("air_temperature_c", self.air_temperature_c, -90, 60)
        number_in_range("wind_speed_10m_m_s", self.wind_speed_10m_m_s, 0, 100)
        number_in_range("cloud_cover_percent", self.cloud_cover_percent, 0, 100)
        number_in_range("precipitation_mm", self.precipitation_mm, 0, 500)


def _parse_date(name: str, value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a YYYY-MM-DD date string") from exc


def _request_json(url: str, timeout_s: float) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Weather API request failed: HTTP {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OSError(f"Weather API request failed: {exc}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError("Weather API returned invalid JSON") from exc


def fetch_hourly_weather(
    location: Location, start_date: str, end_date: str, timeout_s: float = 10,
) -> list[WeatherObservation]:
    """Fetch and parse one contiguous UTC-hourly weather series for a location."""
    start, end = _parse_date("start_date", start_date), _parse_date("end_date", end_date)
    if start > end:
        raise ValueError("start_date must not be after end_date")

    query = urllib.parse.urlencode({
        "latitude": location.latitude_deg,
        "longitude": location.longitude_deg,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": ",".join(_HOURLY_FIELDS),
        "wind_speed_unit": "ms",
        "timezone": "UTC",
    })
    payload = _request_json(f"{_ARCHIVE_URL}?{query}", timeout_s)

    try:
        hourly = payload["hourly"]
        timestamps = hourly["time"]
        series = {field: hourly[field] for field in _HOURLY_FIELDS}
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Weather API response is missing expected fields: {exc}") from exc

    if not isinstance(timestamps, list) or not timestamps or any(
        not isinstance(values, list) for values in series.values()
    ):
        raise ValueError("Weather API response must contain nonempty hourly arrays")
    lengths = {len(timestamps), *(len(values) for values in series.values())}
    if len(lengths) != 1:
        raise ValueError("Weather API response has mismatched field lengths")

    observations = []
    for i, timestamp in enumerate(timestamps):
        row = {field: series[field][i] for field in _HOURLY_FIELDS}
        if any(value is None for value in row.values()):
            raise ValueError(f"Weather API response has missing data at {timestamp}")
        try:
            when = datetime.fromisoformat(timestamp)
            when = (when.replace(tzinfo=timezone.utc) if when.tzinfo is None
                    else when.astimezone(timezone.utc))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Weather API returned an unparseable timestamp: {timestamp}") from exc
        if observations and when <= observations[-1].timestamp_utc:
            raise ValueError("Weather API timestamps must be strictly increasing")
        observations.append(WeatherObservation(
            timestamp_utc=when,
            dni_w_m2=row["direct_normal_irradiance"],
            dhi_w_m2=row["diffuse_radiation"],
            ghi_w_m2=row["shortwave_radiation"],
            air_temperature_c=row["temperature_2m"],
            wind_speed_10m_m_s=row["wind_speed_10m"],
            cloud_cover_percent=row["cloud_cover"],
            precipitation_mm=row["precipitation"],
        ))
    return observations
