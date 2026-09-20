"""Plant site location: geographic coordinates and ground reflectance.

Surface tilt/azimuth live on Layout (they describe the mounting structure);
this module holds where that structure sits on Earth.
"""

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .validation import number_in_range


@dataclass(frozen=True)
class Location:
    name: str
    latitude_deg: float
    longitude_deg: float
    elevation_m: float
    timezone: str
    ground_albedo: float = 0.2

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Location name must be a nonempty string")
        number_in_range("latitude_deg", self.latitude_deg, -90, 90)
        number_in_range("longitude_deg", self.longitude_deg, -180, 180)
        number_in_range("elevation_m", self.elevation_m, -430, 9000)
        number_in_range("ground_albedo", self.ground_albedo, 0, 1)
        if not isinstance(self.timezone, str):
            raise ValueError("timezone must be a string")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA timezone: {self.timezone}") from exc
