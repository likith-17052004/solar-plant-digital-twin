"""Synthetic clear-sky irradiance and temperature - demo support, not a Step.

This exists so the frontend's time/date slider has something physically
reasonable to render instantly, without a network round trip to the real
weather API (weather.py, Step 4) on every drag. It is explicitly illustrative:
the DNI/DHI split and diurnal temperature are coarse approximations, not a
validated clear-sky decomposition model. The real weather path remains the
authoritative one whenever it is used.
"""

from dataclasses import dataclass
import math

from .validation import number_in_range

_DIFFUSE_FRACTION = 0.15

# Clear-sky demo has no cloud cover; weather mode supplies its own value.
SYNTHETIC_CLOUD_COVER_FRACTION = 0.0


@dataclass(frozen=True)
class ClearSkyIrradiance:
    ghi_w_m2: float
    dni_w_m2: float
    dhi_w_m2: float


def estimate_clear_sky_ghi(zenith_deg: float) -> float:
    """Haurwitz (1945) clear-sky global horizontal irradiance.

    GHI = 1098 * cos(Z) * exp(-0.059 / cos(Z)) for Z < 90 deg, else 0. A
    real, simple, citable clear-sky model (the same one pvlib.clearsky.haurwitz
    implements), not a fitted or invented curve.
    """
    number_in_range("zenith_deg", zenith_deg, 0, 180)
    if zenith_deg >= 90:
        return 0.0
    cos_z = math.cos(math.radians(zenith_deg))
    return 1098.0 * cos_z * math.exp(-0.059 / cos_z)


def synthesize_dni_dhi(ghi_w_m2: float, zenith_deg: float) -> ClearSkyIrradiance:
    """Coarse illustrative DNI/DHI split at a fixed 15% diffuse fraction.

    Not a validated decomposition model (contrast the Erbs/Perez-style
    approaches a rigorous clear-sky application would use) - good enough for
    a smooth interactive demo, explicitly not for anything else.
    """
    number_in_range("ghi_w_m2", ghi_w_m2, 0, 1500)
    number_in_range("zenith_deg", zenith_deg, 0, 180)
    if zenith_deg >= 90 or ghi_w_m2 <= 0:
        return ClearSkyIrradiance(ghi_w_m2=max(0.0, ghi_w_m2), dni_w_m2=0.0, dhi_w_m2=max(0.0, ghi_w_m2))
    dhi = _DIFFUSE_FRACTION * ghi_w_m2
    dni = (ghi_w_m2 - dhi) / math.cos(math.radians(zenith_deg))
    return ClearSkyIrradiance(ghi_w_m2=ghi_w_m2, dni_w_m2=dni, dhi_w_m2=dhi)


def synthesize_air_temperature_c(
    local_hour_decimal: float, mean_c: float = 28.0, amplitude_c: float = 8.0, minimum_local_hour: float = 6.0,
) -> float:
    """Simple diurnal sinusoid keyed to local hour - representative of Pavagada, not a forecast."""
    number_in_range("local_hour_decimal", local_hour_decimal, 0, 24)
    phase = 2 * math.pi * (local_hour_decimal - minimum_local_hour) / 24
    return mean_c - amplitude_c * math.cos(phase)
