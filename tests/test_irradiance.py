from dataclasses import replace
import math
import unittest

from solar_twin.irradiance import transpose_to_poa


class IrradianceTests(unittest.TestCase):
    def test_horizontal_surface_recovers_ghi_exactly(self):
        # On tilt=0, POA is definitionally DNI*cos(zenith) + DHI == GHI.
        for zenith in (0, 30, 60, 89):
            dni, dhi = 800, 150
            ghi = dni * math.cos(math.radians(zenith)) + dhi
            result = transpose_to_poa(dni, dhi, ghi, zenith, 150, 0, 180, 0.2)
            with self.subTest(zenith=zenith):
                self.assertAlmostEqual(result.poa_global_w_m2, ghi)
                self.assertEqual(result.poa_ground_diffuse_w_m2, 0)
                self.assertEqual(result.warnings, ())

    def test_sun_below_horizon_has_no_beam_or_warning_free_output(self):
        for zenith in (90, 91, 150):
            result = transpose_to_poa(800, 0, 0, zenith, 180, 25, 180, 0.2)
            self.assertEqual(result.poa_global_w_m2, 0)
            self.assertEqual(result.poa_beam_w_m2, 0)
            self.assertIn("sun_below_horizon", result.warnings)

    def test_south_facing_tilted_surface_matches_worked_example(self):
        zenith_deg, tilt_deg, dni, dhi, albedo = 30, 25, 800, 100, 0.2
        ghi = dni * math.cos(math.radians(zenith_deg)) + dhi
        result = transpose_to_poa(dni, dhi, ghi, zenith_deg, 180, tilt_deg, 180, albedo)

        cos_aoi = math.cos(math.radians(zenith_deg - tilt_deg))  # same-azimuth special case
        expected_beam = dni * cos_aoi
        expected_sky = dhi * (1 + math.cos(math.radians(tilt_deg))) / 2
        expected_ground = ghi * albedo * (1 - math.cos(math.radians(tilt_deg))) / 2

        self.assertAlmostEqual(result.poa_beam_w_m2, expected_beam)
        self.assertAlmostEqual(result.poa_sky_diffuse_w_m2, expected_sky)
        self.assertAlmostEqual(result.poa_ground_diffuse_w_m2, expected_ground)
        self.assertAlmostEqual(result.poa_global_w_m2, expected_beam + expected_sky + expected_ground)
        self.assertAlmostEqual(result.angle_of_incidence_deg, zenith_deg - tilt_deg)

    def test_rejects_bad_input(self):
        base = dict(dni_w_m2=800, dhi_w_m2=100, ghi_w_m2=700, solar_zenith_deg=30,
                    solar_azimuth_deg=180, surface_tilt_deg=25, surface_azimuth_deg=180, ground_albedo=0.2)
        for field, value in (("dni_w_m2", -1), ("solar_zenith_deg", 181), ("surface_tilt_deg", 91),
                             ("ground_albedo", 1.5), ("solar_azimuth_deg", float("nan"))):
            with self.subTest(field=field), self.assertRaises(ValueError):
                transpose_to_poa(**{**base, field: value})


if __name__ == "__main__":
    unittest.main()
