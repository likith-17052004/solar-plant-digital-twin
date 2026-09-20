import math
import unittest

from solar_twin.clear_sky import estimate_clear_sky_ghi, synthesize_air_temperature_c, synthesize_dni_dhi


class ClearSkyTests(unittest.TestCase):
    def test_ghi_peaks_near_zenith_zero_and_matches_haurwitz_formula(self):
        expected = 1098.0 * math.cos(0) * math.exp(-0.059 / math.cos(0))
        self.assertAlmostEqual(estimate_clear_sky_ghi(0), expected)

    def test_ghi_is_zero_at_and_beyond_horizon(self):
        for zenith in (90, 91, 150, 180):
            self.assertEqual(estimate_clear_sky_ghi(zenith), 0)

    def test_ghi_decreases_monotonically_with_zenith(self):
        values = [estimate_clear_sky_ghi(z) for z in range(0, 90, 5)]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_dni_dhi_split_recovers_ghi_via_horizontal_identity(self):
        zenith = 30
        ghi = estimate_clear_sky_ghi(zenith)
        split = synthesize_dni_dhi(ghi, zenith)
        recovered_ghi = split.dni_w_m2 * math.cos(math.radians(zenith)) + split.dhi_w_m2
        self.assertAlmostEqual(recovered_ghi, ghi)
        self.assertAlmostEqual(split.dhi_w_m2, 0.15 * ghi)

    def test_dni_dhi_split_is_zero_at_night(self):
        split = synthesize_dni_dhi(0, 150)
        self.assertEqual(split.dni_w_m2, 0)
        self.assertEqual(split.dhi_w_m2, 0)

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            estimate_clear_sky_ghi(float("nan"))
        with self.assertRaises(ValueError):
            synthesize_dni_dhi(-1, 30)
        with self.assertRaises(ValueError):
            synthesize_air_temperature_c(25)

    def test_diurnal_temperature_stays_within_configured_band(self):
        values = [synthesize_air_temperature_c(h) for h in range(0, 24)]
        self.assertLessEqual(max(values), 28.0 + 8.0)
        self.assertGreaterEqual(min(values), 28.0 - 8.0)
        self.assertAlmostEqual(synthesize_air_temperature_c(6.0), 28.0 - 8.0)


if __name__ == "__main__":
    unittest.main()
