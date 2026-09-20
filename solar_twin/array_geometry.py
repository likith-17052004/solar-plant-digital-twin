"""Step 8: row-to-row shading and incidence-angle losses on the array plane.

`irradiance.transpose_to_poa` answers "how much light reaches a tilted plane
floating in the open?". A real array is a field of parallel rows that shade
each other at low sun, behind glass that reflects light away at a grazing
angle. Both effects are pure geometry, both are always losses, and neither
was modeled before this step - so reported POA was optimistic exactly when
the sun was low.

Two named, published models, layered over the unmodified Step 4 POA result:

* Row-to-row shading for infinite parallel rows (Passias & Kallback, 1984),
  the same geometry as `pvlib.shading.shaded_fraction_1d`. Beam only.
* ASHRAE incidence angle modifier, IAM = 1 - b0 (1/cos(aoi) - 1), the same
  model as `pvlib.iam.ashrae`, with b0 = 0.05 for uncoated glass.

Honest limits, all recorded in `assumptions` on the result:

* Shading is converted to lost beam *proportionally to shaded area*. Real
  arrays lose far more than that, because one shaded cell throttles its whole
  series string. Electrical mismatch under partial shade is not modeled, so
  low-sun output here is still an upper bound.
* Rows are treated as infinitely long and coplanar on flat ground. Edge rows,
  which are unshaded on one side, get no credit; terrain slope is ignored.
* Diffuse light is NOT reduced. Neighbouring rows really do mask part of the
  sky dome and part of the ground, so sky- and ground-diffuse are also
  overestimated here by a few percent. Deliberately left out rather than
  guessed at.
* IAM is applied to beam only. Diffuse arrives from every direction at once,
  so it needs an effective-angle treatment (Brandemuehl & Beckman) that is
  not implemented here.
"""

from dataclasses import dataclass
import math

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
    def shading_onset_profile_angle_deg(self) -> float:
        """Profile angle below which the row in front starts to shade this one."""
        tilt = math.radians(self.tilt_deg)
        clear = self.row_pitch_m / self.collector_slant_m - math.cos(tilt)
        if clear <= 0:
            return 90.0
        return math.degrees(math.atan(math.sin(tilt) / clear))


def profile_angle_deg(
    solar_zenith_deg: float, solar_azimuth_deg: float, surface_azimuth_deg: float,
) -> float | None:
    """Sun elevation projected into the plane perpendicular to the rows.

    This is the angle that governs how long a row's shadow is in the only
    direction that matters - across the rows. Returns None when the sun is
    below the horizon, or azimuthally behind the array plane (in which case
    the row in front cannot shade this one; see module docstring).
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
    """Fraction of a row's slant height shaded by the row in front of it.

    Passias & Kallback (1984). Deriving it from the 2D cross-section: a ray
    grazing the top edge of the row in front crosses this row at slant
    distance u = L - P / (cos(tilt) + sin(tilt) / tan(profile angle)) from its
    lower edge, so everything below u is in shadow.
    """
    profile = profile_angle_deg(solar_zenith_deg, solar_azimuth_deg, geometry.surface_azimuth_deg)
    if profile is None:
        return 0.0
    tilt = math.radians(geometry.tilt_deg)
    tangent = math.tan(math.radians(profile))
    if tangent <= 1e-9:
        return 1.0
    reach = math.cos(tilt) + math.sin(tilt) / tangent
    return max(0.0, min(1.0, 1 - geometry.row_pitch_m / (geometry.collector_slant_m * reach)))


def ashrae_iam(angle_of_incidence_deg: float, b0: float = ASHRAE_B0_UNCOATED_GLASS) -> float:
    """Transmission relative to normal incidence. 1.0 head-on, 0.0 edge-on."""
    number_in_range("angle_of_incidence_deg", angle_of_incidence_deg, 0, 180)
    number_in_range("b0", b0, 0, 1)
    if angle_of_incidence_deg >= 90:
        return 0.0
    cosine = math.cos(math.radians(angle_of_incidence_deg))
    return max(0.0, min(1.0, 1 - b0 * (1 / cosine - 1)))


@dataclass(frozen=True)
class ArrayPlaneResult:
    """POA after row shading and glass reflection - what the cells actually see."""

    incident: POAResult
    shaded_row_fraction: float
    profile_angle_deg: float | None
    incidence_angle_modifier: float
    effective_beam_w_m2: float
    effective_poa_global_w_m2: float
    shading_loss_w_m2: float
    reflection_loss_w_m2: float
    ground_coverage_ratio: float
    warnings: tuple[str, ...]
    scope: str = "Optical losses on the array plane; electrical mismatch under shade is not modeled"
    assumptions: tuple[str, ...] = (
        "row_shading_reduces_beam_in_proportion_to_shaded_area_string_level_mismatch_not_modeled",
        "rows_treated_as_infinitely_long_and_coplanar_on_flat_ground_edge_rows_get_no_credit",
        "sky_and_ground_diffuse_not_reduced_for_row_to_row_masking_so_diffuse_is_overestimated",
        "incidence_angle_modifier_applied_to_beam_only_diffuse_effective_angle_not_modeled",
    )


def apply_array_losses(
    incident: POAResult,
    solar_zenith_deg: float,
    solar_azimuth_deg: float,
    geometry: RowGeometry,
    b0: float = ASHRAE_B0_UNCOATED_GLASS,
) -> ArrayPlaneResult:
    """Layer shading and reflection onto an unmodified Step 4 POA result."""
    profile = profile_angle_deg(solar_zenith_deg, solar_azimuth_deg, geometry.surface_azimuth_deg)
    shaded = shaded_fraction(solar_zenith_deg, solar_azimuth_deg, geometry)
    iam = ashrae_iam(incident.angle_of_incidence_deg, b0)

    beam = incident.poa_beam_w_m2
    after_shading = beam * (1 - shaded)
    effective_beam = after_shading * iam
    diffuse = incident.poa_sky_diffuse_w_m2 + incident.poa_ground_diffuse_w_m2

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
        effective_beam_w_m2=effective_beam,
        effective_poa_global_w_m2=effective_beam + diffuse,
        shading_loss_w_m2=beam - after_shading,
        reflection_loss_w_m2=after_shading - effective_beam,
        ground_coverage_ratio=geometry.ground_coverage_ratio,
        warnings=tuple(dict.fromkeys(warnings)),
    )
