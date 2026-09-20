from datetime import datetime, timezone
import math
import unittest

from solar_twin.irradiance import transpose_to_poa

NOON = datetime(2025, 6, 1, 6, 30, tzinfo=timezone.utc)


def consistent(dni, dhi, zenith):
    """GHI implied by a DNI/DHI pair, so the three inputs agree."""
    return dni * math.cos(math.radians(zenith)) + dhi


class TranspositionTests(unittest.TestCase):
    def test_horizontal_surface_recovers_ghi(self):
        # On tilt = 0 the plane sees exactly the horizontal irradiance, under
        # either sky model.
        for model in ("isotropic", "perez"):
            for zenith in (0, 30, 60, 85):
                dni, dhi = 800, 150
                ghi = consistent(dni, dhi, zenith)
                result = transpose_to_poa(dni, dhi, ghi, zenith, 150, 0, 180, 0.2, NOON, model)
                with self.subTest(model=model, zenith=zenith):
                    self.assertAlmostEqual(result.poa_global_w_m2, ghi, places=6)
                    self.assertEqual(result.poa_ground_diffuse_w_m2, 0)

    def test_sun_below_horizon_has_no_beam(self):
        for zenith in (90, 91, 150):
            result = transpose_to_poa(800, 0, 0, zenith, 180, 25, 180, 0.2, NOON)
            with self.subTest(zenith=zenith):
                self.assertEqual(result.poa_beam_w_m2, 0)
                self.assertEqual(result.poa_global_w_m2, 0)
                self.assertIn("sun_below_horizon", result.warnings)

    def test_isotropic_matches_the_liu_and_jordan_worked_example(self):
        # The closed form this module used to implement by hand, kept as a
        # check that pvlib's isotropic model is the same thing.
        zenith_deg, tilt_deg, dni, dhi, albedo = 30, 25, 800, 100, 0.2
        ghi = consistent(dni, dhi, zenith_deg)
        result = transpose_to_poa(dni, dhi, ghi, zenith_deg, 180, tilt_deg, 180, albedo,
                                  NOON, "isotropic")
        cos_aoi = math.cos(math.radians(zenith_deg - tilt_deg))   # same-azimuth special case
        self.assertAlmostEqual(result.poa_beam_w_m2, dni * cos_aoi, places=6)
        self.assertAlmostEqual(result.poa_sky_diffuse_w_m2,
                               dhi * (1 + math.cos(math.radians(tilt_deg))) / 2, places=6)
        self.assertAlmostEqual(result.poa_ground_diffuse_w_m2,
                               ghi * albedo * (1 - math.cos(math.radians(tilt_deg))) / 2, places=6)
        self.assertAlmostEqual(result.angle_of_incidence_deg, zenith_deg - tilt_deg, places=6)

    def test_perez_differs_from_isotropic_on_a_tilted_plane(self):
        # The reason for using it. On a horizontal surface the two agree; the
        # anisotropic sky only matters once the plane is tilted.
        dni, dhi, zenith = 800, 150, 45
        ghi = consistent(dni, dhi, zenith)
        flat = [transpose_to_poa(dni, dhi, ghi, zenith, 180, 0, 180, 0.2, NOON, m).poa_sky_diffuse_w_m2
                for m in ("isotropic", "perez")]
        tilted = [transpose_to_poa(dni, dhi, ghi, zenith, 180, 25, 180, 0.2, NOON, m).poa_sky_diffuse_w_m2
                  for m in ("isotropic", "perez")]
        self.assertAlmostEqual(flat[0], flat[1], places=6)
        self.assertNotAlmostEqual(tilted[0], tilted[1], places=1)

    def test_falls_back_to_isotropic_without_a_timestamp_and_says_so(self):
        # Perez needs extraterrestrial irradiance and air mass, both of which
        # need a date. Silently returning a different model's answer would be
        # worse than saying so.
        result = transpose_to_poa(800, 150, 700, 30, 180, 25, 180, 0.2)
        self.assertEqual(result.model, "isotropic")
        self.assertIn("no_timestamp_supplied_fell_back_to_isotropic_transposition", result.warnings)

    def test_reports_which_model_was_used(self):
        self.assertEqual(transpose_to_poa(800, 150, 700, 30, 180, 25, 180, 0.2, NOON).model, "perez")

    def test_no_diffuse_light_falls_back_rather_than_dividing_by_zero(self):
        # Perez forms a ratio with DHI underneath; pvlib divides by zero on
        # scalars. With no diffuse sky there is nothing to distribute anyway.
        result = transpose_to_poa(800, 0, 692.8, 30, 180, 25, 180, 0.2, NOON, "perez")
        self.assertEqual(result.model, "isotropic")
        self.assertEqual(result.poa_sky_diffuse_w_m2, 0)
        self.assertGreater(result.poa_beam_w_m2, 0)

    def test_rejects_bad_input(self):
        base = dict(dni_w_m2=800, dhi_w_m2=100, ghi_w_m2=700, solar_zenith_deg=30,
                    solar_azimuth_deg=180, surface_tilt_deg=25, surface_azimuth_deg=180,
                    ground_albedo=0.2)
        for field, value in (("dni_w_m2", -1), ("solar_zenith_deg", 181), ("surface_tilt_deg", 91),
                             ("ground_albedo", 1.5), ("solar_azimuth_deg", float("nan"))):
            with self.subTest(field=field), self.assertRaises(ValueError):
                transpose_to_poa(**{**base, field: value})


if __name__ == "__main__":
    unittest.main()
