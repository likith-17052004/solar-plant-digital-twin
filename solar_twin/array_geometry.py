"""Row-to-row shading, diffuse masking and incidence-angle losses, via pvlib.

`irradiance.transpose_to_poa` answers "how much light reaches a tilted plane
floating in the open?". A real array is a field of parallel rows that shade
each other at low sun, mask part of each other's view of the sky, and sit
behind glass that reflects light away at a grazing angle. All three are pure
geometry, all three are always losses.

Three pvlib models, layered over an unmodified transposition result:

* `shading.shaded_fraction1d` - beam shading by the row in front, the Passias
  & Kallback (1984) geometry.
* `shading.masking_angle_passias` + `shading.sky_diffuse_passias` - the share
  of the sky dome each row hides from its neighbour.
* `iam.ashrae` - reflection at the glass, IAM = 1 - b0 (1/cos(aoi) - 1).

The first and last were previously written out by hand here, and both matched
pvlib exactly when checked - beam shaded fraction to four decimals across a
sunset, IAM to 0.00e+00 at every angle. The diffuse masking is genuinely new:
it was documented as omitted, with a guess that it cost "a few percent". It
does not. At this array's GCR of 0.35 it is **0.22%** of sky diffuse, and the
guess was wrong by an order of magnitude. It reaches 2% only around GCR 0.74.

Honest limits that remain:

* Shading is converted to lost beam *proportionally to shaded area*. Real
  arrays lose far more, because one shaded cell throttles its whole series
  string. Electrical mismatch under partial shade is still not modeled, so
  low-sun output here remains an upper bound.
* Rows are treated as infinitely long and coplanar on flat ground. Edge rows,
  unshaded on one side, get no credit; terrain slope is ignored.
* IAM is applied to beam only. Diffuse arrives from every direction at once
  and needs an effective-angle treatment that is not implemented here.
* Ground-reflected irradiance is not reduced for the rows blocking the view
  of the ground, which is the mirror of the sky masking above.
"""

from dataclasses import dataclass
import math

import pvlib

from .irradiance import POAResult
from .validation import number_in_range

ASHRAE_B0_UNCOATED_GLASS = 0.05


@dataclass(frozen=True)
class RowGeometry:
    """Cross-section of one repeating row pair, in metres."""

    collector_slant_m: float
    row_pitch_m: float
    tilt_deg: float
    surface_azimuth_deg: float

    def __post_init__(self):
        number_in_range("collector_slant_m", self.collector_slant_m, 0.1, 100)
        number_in_range("row_pitch_m", self.row_pitch_m, 0.1, 1000)
        number_in_range("tilt_deg", self.tilt_deg, 0, 90)
        number_in_range("surface_azimuth_deg", self.surface_azimuth_deg, 0, 360)
        if self.row_pitch_m < self.collector_slant_m * math.cos(math.radians(self.tilt_deg)):
            raise ValueError("Row pitch is shorter than the row's own horizontal footprint")

    @property
    def ground_coverage_ratio(self) -> float:
        """GCR: collector area per unit ground area. Industry convention is slant/pitch."""
        return self.collector_slant_m / self.row_pitch_m

    @property
    def axis_azimuth_deg(self) -> float:
        """Bearing the rows run along - pvlib's frame is the axis, not the face."""
        return (self.surface_azimuth_deg - 90) % 360

    @property
    def shading_onset_profile_angle_deg(self) -> float:
        """Profile angle below which the row in front starts to shade this one."""
        tilt = math.radians(self.tilt_deg)
        clear = self.row_pitch_m / self.collector_slant_m - math.cos(tilt)
        if clear <= 0:
            return 90.0
        return math.degrees(math.atan(math.sin(tilt) / clear))

    @property
    def sky_diffuse_masking_fraction(self) -> float:
        """Share of sky diffuse hidden by the neighbouring rows."""
        angle = pvlib.shading.masking_angle_passias(self.tilt_deg, self.ground_coverage_ratio)
        return float(pvlib.shading.sky_diffuse_passias(angle))


def profile_angle_deg(
    solar_zenith_deg: float, solar_azimuth_deg: float, surface_azimuth_deg: float,
) -> float | None:
    """Sun elevation projected into the plane perpendicular to the rows.

    This is the angle that governs how long a row's shadow is in the only
    direction that matters - across the rows. Returns None when the sun is
    below the horizon, or azimuthally behind the array plane (in which case
    the row in front cannot shade this one).
    """
    elevation = 90.0 - solar_zenith_deg
    if elevation <= 0:
        return None
    across = math.cos(math.radians(solar_azimuth_deg - surface_azimuth_deg))
    if across <= 0:
        return None
    return math.degrees(math.atan2(math.tan(math.radians(elevation)), across))


def shaded_fraction(
    solar_zenith_deg: float, solar_azimuth_deg: float, geometry: RowGeometry,
) -> float:
    """Fraction of a row's slant height shaded by the row in front of it."""
    if profile_angle_deg(solar_zenith_deg, solar_azimuth_deg, geometry.surface_azimuth_deg) is None:
        return 0.0
    value = float(pvlib.shading.shaded_fraction1d(
        solar_zenith_deg, solar_azimuth_deg,
        axis_azimuth=geometry.axis_azimuth_deg,
        shaded_row_rotation=geometry.tilt_deg,
        collector_width=geometry.collector_slant_m,
        pitch=geometry.row_pitch_m,
    ))
    if value != value:
        return 0.0
    return max(0.0, min(1.0, value))


def ashrae_iam(angle_of_incidence_deg: float, b0: float = ASHRAE_B0_UNCOATED_GLASS) -> float:
    """Transmission relative to normal incidence. 1.0 head-on, 0.0 edge-on."""
    number_in_range("angle_of_incidence_deg", angle_of_incidence_deg, 0, 180)
    number_in_range("b0", b0, 0, 1)
    if angle_of_incidence_deg >= 90:
        return 0.0
    return max(0.0, min(1.0, float(pvlib.iam.ashrae(angle_of_incidence_deg, b=b0))))


@dataclass(frozen=True)
class ArrayPlaneResult:
    """POA after row shading, sky masking and glass reflection."""

    incident: POAResult
    shaded_row_fraction: float
    profile_angle_deg: float | None
    incidence_angle_modifier: float
    sky_masking_fraction: float
    effective_beam_w_m2: float
    effective_sky_diffuse_w_m2: float
    effective_poa_global_w_m2: float
    shading_loss_w_m2: float
    reflection_loss_w_m2: float
    sky_masking_loss_w_m2: float
    ground_coverage_ratio: float
    warnings: tuple[str, ...]
    scope: str = "Optical losses on the array plane; electrical mismatch under shade is not modeled"
    assumptions: tuple[str, ...] = (
        "row_shading_reduces_beam_in_proportion_to_shaded_area_string_level_mismatch_not_modeled",
        "rows_treated_as_infinitely_long_and_coplanar_on_flat_ground_edge_rows_get_no_credit",
        "incidence_angle_modifier_applied_to_beam_only_diffuse_effective_angle_not_modeled",
        "ground_reflected_irradiance_not_reduced_for_rows_blocking_the_view_of_the_ground",
    )


def apply_array_losses(
    incident: POAResult,
    solar_zenith_deg: float,
    solar_azimuth_deg: float,
    geometry: RowGeometry,
    b0: float = ASHRAE_B0_UNCOATED_GLASS,
) -> ArrayPlaneResult:
    """Layer shading, sky masking and reflection onto a transposition result."""
    profile = profile_angle_deg(solar_zenith_deg, solar_azimuth_deg, geometry.surface_azimuth_deg)
    shaded = shaded_fraction(solar_zenith_deg, solar_azimuth_deg, geometry)
    iam = ashrae_iam(incident.angle_of_incidence_deg, b0)
    masking = geometry.sky_diffuse_masking_fraction

    beam = incident.poa_beam_w_m2
    after_shading = beam * (1 - shaded)
    effective_beam = after_shading * iam
    sky = incident.poa_sky_diffuse_w_m2
    effective_sky = sky * (1 - masking)
    ground = incident.poa_ground_diffuse_w_m2

    warnings = list(incident.warnings)
    if shaded > 0:
        warnings.append("beam_partially_blocked_by_row_in_front")
    if shaded >= 0.99 and beam > 0:
        warnings.append("row_fully_shaded_remaining_poa_is_diffuse_only")

    return ArrayPlaneResult(
        incident=incident,
        shaded_row_fraction=shaded,
        profile_angle_deg=profile,
        incidence_angle_modifier=iam,
        sky_masking_fraction=masking,
        effective_beam_w_m2=effective_beam,
        effective_sky_diffuse_w_m2=effective_sky,
        effective_poa_global_w_m2=effective_beam + effective_sky + ground,
        shading_loss_w_m2=beam - after_shading,
        reflection_loss_w_m2=after_shading - effective_beam,
        sky_masking_loss_w_m2=sky - effective_sky,
        ground_coverage_ratio=geometry.ground_coverage_ratio,
        warnings=tuple(dict.fromkeys(warnings)),
    )
