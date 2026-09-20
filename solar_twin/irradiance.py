"""Transposition of DNI/DHI/GHI onto a tilted, oriented surface, via pvlib.

Perez et al. (1990), as implemented by `pvlib.irradiance.get_total_irradiance`
with `model="perez"`. The sky dome is not uniformly bright: there is a bright
halo around the sun (circumsolar) and a bright band at the horizon, and Perez
models both from the measured DNI/DHI plus air mass.

This replaced a hand-written Liu & Jordan isotropic model, which treats sky
diffuse as uniform in every direction. Isotropic is the standard *starting*
model and it is exact on a horizontal surface, but on a tilted plane it is
systematically wrong - and the whole point of this module is a tilted plane.
Perez is the model the industry actually uses for tilted-surface estimates.

Both models are still reachable through `model=`, because being able to show
the difference is worth more than hiding it. On this array the gap is real:
see `tests/test_irradiance.py`, which pins it.
"""

from dataclasses import dataclass
from datetime import datetime

import pvlib

from .validation import number_in_range


@dataclass(frozen=True)
class POAResult:
    poa_global_w_m2: float
    poa_beam_w_m2: float
    poa_sky_diffuse_w_m2: float
    poa_ground_diffuse_w_m2: float
    angle_of_incidence_deg: float
    model: str
    warnings: tuple[str, ...]


def transpose_to_poa(
    dni_w_m2: float,
    dhi_w_m2: float,
    ghi_w_m2: float,
    solar_zenith_deg: float,
    solar_azimuth_deg: float,
    surface_tilt_deg: float,
    surface_azimuth_deg: float,
    ground_albedo: float,
    timestamp_utc: datetime | None = None,
    model: str = "perez",
) -> POAResult:
    """Plane-of-array irradiance, split into beam, sky diffuse and ground diffuse.

    `timestamp_utc` is needed by Perez, which scales circumsolar and horizon
    brightening against extraterrestrial irradiance and air mass. Without one
    the call falls back to isotropic and says so in `warnings`, rather than
    silently returning a different model's answer.
    """
    number_in_range("dni_w_m2", dni_w_m2, 0, 1500)
    number_in_range("dhi_w_m2", dhi_w_m2, 0, 1500)
    number_in_range("ghi_w_m2", ghi_w_m2, 0, 1500)
    number_in_range("solar_zenith_deg", solar_zenith_deg, 0, 180)
    number_in_range("solar_azimuth_deg", solar_azimuth_deg, 0, 360)
    number_in_range("surface_tilt_deg", surface_tilt_deg, 0, 90)
    number_in_range("surface_azimuth_deg", surface_azimuth_deg, 0, 360)
    number_in_range("ground_albedo", ground_albedo, 0, 1)

    warnings = []
    night = solar_zenith_deg >= 90
    if night:
        warnings.append("sun_below_horizon")
        # pvlib projects whatever DNI it is handed onto the plane by geometry
        # alone, and for a tilted surface that projection stays positive a
        # little past sunset. There is no beam once the sun is down, so it is
        # zeroed here rather than trusting the caller to pass DNI = 0.
        dni_w_m2 = 0.0

    effective_model = model
    extra = airmass = None
    if model == "perez":
        if dhi_w_m2 <= 0:
            # Perez forms a sky-clearness ratio with DHI in the denominator, so
            # it is undefined without any diffuse light. pvlib guards this for
            # arrays but divides by zero on scalars. There is no diffuse sky to
            # distribute anyway, so isotropic gives the same (zero) answer.
            effective_model = "isotropic"
        elif timestamp_utc is None:
            effective_model = "isotropic"
            warnings.append("no_timestamp_supplied_fell_back_to_isotropic_transposition")
        else:
            extra = pvlib.irradiance.get_extra_radiation(timestamp_utc)
            airmass = pvlib.atmosphere.get_relative_airmass(solar_zenith_deg)
            # Air mass is undefined below the horizon; Perez needs a finite
            # value, and at night every diffuse term is zero anyway.
            if airmass is None or airmass != airmass:
                airmass = 40.0

    total = pvlib.irradiance.get_total_irradiance(
        surface_tilt=surface_tilt_deg,
        surface_azimuth=surface_azimuth_deg,
        solar_zenith=solar_zenith_deg,
        solar_azimuth=solar_azimuth_deg,
        dni=dni_w_m2, ghi=ghi_w_m2, dhi=dhi_w_m2,
        dni_extra=extra, airmass=airmass,
        albedo=ground_albedo, model=effective_model,
    )
    angle_of_incidence = float(pvlib.irradiance.aoi(
        surface_tilt_deg, surface_azimuth_deg, solar_zenith_deg, solar_azimuth_deg))

    def value(key: str) -> float:
        raw = float(total[key])
        # Perez can return a small negative horizon term at extreme geometry.
        return max(0.0, 0.0 if raw != raw else raw)

    return POAResult(
        poa_global_w_m2=value("poa_global"),
        poa_beam_w_m2=value("poa_direct"),
        poa_sky_diffuse_w_m2=value("poa_sky_diffuse"),
        poa_ground_diffuse_w_m2=value("poa_ground_diffuse"),
        angle_of_incidence_deg=angle_of_incidence,
        model=effective_model,
        warnings=tuple(warnings),
    )
