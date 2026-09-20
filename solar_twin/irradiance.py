"""Isotropic-sky transposition of DNI/DHI/GHI onto a tilted, oriented surface.

Liu & Jordan (1963); the same model as pvlib's
`pvlib.irradiance.get_total_irradiance(model="isotropic")`. It treats sky
diffuse as uniform across the sky dome, so it omits circumsolar brightening
and horizon brightening that anisotropic models (e.g. Perez) add. This is a
documented simplification, not a bug: it is the standard starting model and
is exact by construction on a horizontal surface (see tests).
"""

from dataclasses import dataclass
import math

from .validation import number_in_range


@dataclass(frozen=True)
class POAResult:
    poa_global_w_m2: float
    poa_beam_w_m2: float
    poa_sky_diffuse_w_m2: float
    poa_ground_diffuse_w_m2: float
    angle_of_incidence_deg: float
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
) -> POAResult:
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

    zenith, tilt = math.radians(solar_zenith_deg), math.radians(surface_tilt_deg)
    azimuth_difference = math.radians(solar_azimuth_deg - surface_azimuth_deg)
    cos_aoi = (math.cos(zenith) * math.cos(tilt)
               + math.sin(zenith) * math.sin(tilt) * math.cos(azimuth_difference))
    angle_of_incidence = math.degrees(math.acos(max(-1.0, min(1.0, cos_aoi))))

    beam = 0.0 if night else dni_w_m2 * max(0.0, cos_aoi)
    sky_diffuse = dhi_w_m2 * (1 + math.cos(tilt)) / 2
    ground_diffuse = ghi_w_m2 * ground_albedo * (1 - math.cos(tilt)) / 2
    return POAResult(
        poa_global_w_m2=beam + sky_diffuse + ground_diffuse,
        poa_beam_w_m2=beam,
        poa_sky_diffuse_w_m2=sky_diffuse,
        poa_ground_diffuse_w_m2=ground_diffuse,
        angle_of_incidence_deg=angle_of_incidence,
        warnings=tuple(warnings),
    )
