import math
import unittest

from solar_twin.array_geometry import (
    RowGeometry, apply_array_losses, ashrae_iam, profile_angle_deg, shaded_fraction,
)
from solar_twin.irradiance import transpose_to_poa

# The configured Pavagada array: one 2.384 m module up the slope, 6.8 m pitch,
# 25 degrees, facing due south. GCR 0.3506.
ROWS = RowGeometry(collector_slant_m=2.384, row_pitch_m=6.8, tilt_deg=25.0, surface_azimuth_deg=180.0)


class RowGeometryTests(unittest.TestCase):
    def test_ground_coverage_ratio(self):
        self.assertAlmostEqual(ROWS.ground_coverage_ratio, 2.384 / 6.8, places=6)

    def test_rejects_pitch_shorter_than_the_rows_own_footprint(self):
        with self.assertRaisesRegex(ValueError, "Row pitch"):
            RowGeometry(collector_slant_m=4.0, row_pitch_m=3.0, tilt_deg=25.0, surface_azimuth_deg=180.0)

    def test_flat_rows_never_shade(self):
        flat = RowGeometry(collector_slant_m=2.0, row_pitch_m=4.0, tilt_deg=0.0, surface_azimuth_deg=180.0)
        self.assertEqual(flat.shading_onset_profile_angle_deg, 0.0)
        self.assertEqual(shaded_fraction(85.0, 180.0, flat), 0.0)


class ProfileAngleTests(unittest.TestCase):
    def test_sun_in_the_plane_of_the_normal_gives_back_its_own_elevation(self):
        # Sun due south, same azimuth as the surface: the projection is identity.
        self.assertAlmostEqual(profile_angle_deg(60.0, 180.0, 180.0), 30.0, places=6)

    def test_sun_off_axis_projects_to_a_steeper_profile_angle(self):
        # Swinging the sun toward the east raises the across-row projection.
        self.assertGreater(profile_angle_deg(60.0, 120.0, 180.0), 30.0)

    def test_below_horizon_and_behind_the_array_return_none(self):
        self.assertIsNone(profile_angle_deg(91.0, 180.0, 180.0))
        self.assertIsNone(profile_angle_deg(30.0, 0.0, 180.0))


class ShadedFractionTests(unittest.TestCase):
    def test_high_sun_is_unshaded(self):
        self.assertEqual(shaded_fraction(20.0, 180.0, ROWS), 0.0)

    def test_onset_angle_is_the_boundary_between_shaded_and_clear(self):
        onset = ROWS.shading_onset_profile_angle_deg
        self.assertAlmostEqual(onset, 12.2526, places=3)
        just_above = shaded_fraction(90.0 - (onset + 0.5), 180.0, ROWS)
        just_below = shaded_fraction(90.0 - (onset - 0.5), 180.0, ROWS)
        self.assertEqual(just_above, 0.0)
        self.assertGreater(just_below, 0.0)

    def test_matches_hand_computed_geometry_at_five_degrees(self):
        # u/L = 1 - P / (L (cos b + sin b / tan psi)), with psi = elevation
        # because the sun is due south of a south-facing row.
        tilt = math.radians(25.0)
        reach = math.cos(tilt) + math.sin(tilt) / math.tan(math.radians(5.0))
        self.assertAlmostEqual(shaded_fraction(85.0, 180.0, ROWS), 1 - 6.8 / (2.384 * reach), places=9)

    def test_shading_grows_monotonically_as_the_sun_sets(self):
        values = [shaded_fraction(90.0 - e, 180.0, ROWS) for e in (12.0, 9.0, 6.0, 3.0, 1.0)]
        self.assertEqual(values, sorted(values))
        self.assertLessEqual(values[-1], 1.0)

    def test_sun_below_horizon_is_not_reported_as_shaded(self):
        self.assertEqual(shaded_fraction(95.0, 180.0, ROWS), 0.0)


class IncidenceAngleModifierTests(unittest.TestCase):
    def test_normal_incidence_transmits_fully(self):
        self.assertEqual(ashrae_iam(0.0), 1.0)

    def test_published_ashrae_values(self):
        for aoi in (40.0, 60.0, 80.0):
            expected = 1 - 0.05 * (1 / math.cos(math.radians(aoi)) - 1)
            self.assertAlmostEqual(ashrae_iam(aoi), expected, places=9)

    def test_grazing_and_rear_incidence_transmit_nothing(self):
        self.assertEqual(ashrae_iam(90.0), 0.0)
        self.assertEqual(ashrae_iam(150.0), 0.0)

    def test_decreases_monotonically(self):
        values = [ashrae_iam(a) for a in (0.0, 20.0, 40.0, 60.0, 75.0, 89.0)]
        self.assertEqual(values, sorted(values, reverse=True))


class ArrayPlaneTests(unittest.TestCase):
    def poa(self, zenith, azimuth):
        return transpose_to_poa(dni_w_m2=800, dhi_w_m2=120, ghi_w_m2=400,
                                solar_zenith_deg=zenith, solar_azimuth_deg=azimuth,
                                surface_tilt_deg=25.0, surface_azimuth_deg=180.0, ground_albedo=0.2)

    def test_losses_are_never_gains(self):
        for zenith, azimuth in ((20.0, 180.0), (60.0, 140.0), (85.0, 100.0), (89.0, 95.0)):
            with self.subTest(zenith=zenith):
                incident = self.poa(zenith, azimuth)
                result = apply_array_losses(incident, zenith, azimuth, ROWS)
                self.assertLessEqual(result.effective_poa_global_w_m2, incident.poa_global_w_m2 + 1e-9)
                self.assertGreaterEqual(result.shading_loss_w_m2, -1e-9)
                self.assertGreaterEqual(result.reflection_loss_w_m2, -1e-9)

    def test_only_a_sliver_of_beam_survives_at_grazing_sun(self):
        # Even at 0.05 degrees elevation the very top of the row still sees the
        # sun, so beam goes nearly - but not exactly - to zero. What is left of
        # POA is diffuse.
        zenith = 89.95
        incident = self.poa(zenith, 180.0)
        result = apply_array_losses(incident, zenith, 180.0, ROWS)
        diffuse = incident.poa_sky_diffuse_w_m2 + incident.poa_ground_diffuse_w_m2
        self.assertGreater(result.shaded_row_fraction, 0.99)
        self.assertLess(result.effective_beam_w_m2, 0.01 * incident.poa_beam_w_m2)
        self.assertAlmostEqual(result.effective_poa_global_w_m2, diffuse,
                               delta=0.01 * incident.poa_beam_w_m2)
        self.assertIn("row_fully_shaded_remaining_poa_is_diffuse_only", result.warnings)

    def test_partial_shade_at_half_a_degree_elevation_is_not_reported_as_total(self):
        # 94 percent shaded is not 100 percent; the honest warning must not fire.
        result = apply_array_losses(self.poa(89.5, 180.0), 89.5, 180.0, ROWS)
        self.assertAlmostEqual(result.shaded_row_fraction, 0.942, places=2)
        self.assertNotIn("row_fully_shaded_remaining_poa_is_diffuse_only", result.warnings)
        self.assertIn("beam_partially_blocked_by_row_in_front", result.warnings)

    def test_high_sun_loses_nothing_to_shading_but_still_reflects(self):
        incident = self.poa(20.0, 180.0)
        result = apply_array_losses(incident, 20.0, 180.0, ROWS)
        self.assertEqual(result.shaded_row_fraction, 0.0)
        self.assertEqual(result.shading_loss_w_m2, 0.0)
        self.assertGreater(result.reflection_loss_w_m2, 0.0)
        self.assertLess(result.incidence_angle_modifier, 1.0)

    def test_energy_budget_closes(self):
        incident = self.poa(80.0, 150.0)
        result = apply_array_losses(incident, 80.0, 150.0, ROWS)
        self.assertAlmostEqual(
            incident.poa_beam_w_m2,
            result.effective_beam_w_m2 + result.shading_loss_w_m2 + result.reflection_loss_w_m2,
            places=9,
        )

    def test_incident_warnings_are_carried_forward(self):
        incident = self.poa(95.0, 270.0)
        result = apply_array_losses(incident, 95.0, 270.0, ROWS)
        self.assertIn("sun_below_horizon", result.warnings)


if __name__ == "__main__":
    unittest.main()
