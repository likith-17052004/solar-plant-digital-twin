from datetime import datetime, timezone
import math
from pathlib import Path
import unittest

from solar_twin.clear_sky import (
    SYNTHETIC_CLOUD_COVER_FRACTION, ClearSkyIrradiance, clear_sky_irradiance,
    synthesize_air_temperature_c,
)
from solar_twin.plant import load_plant
from solar_twin.solar_position import solar_position

CONFIG = Path(__file__).resolve().parents[1] / "config" / "plant.json"


class ClearSkyTests(unittest.TestCase):
    def setUp(self):
        self.location = load_plant(CONFIG).location

    def at(self, hour_utc, minute=30, month=6, day=1):
        when = datetime(2025, month, day, hour_utc, minute, tzinfo=timezone.utc)
        zenith = solar_position(self.location, when).zenith_deg
        return when, zenith, clear_sky_irradiance(self.location, when, zenith)

    def test_night_is_dark(self):
        _, zenith, sky = self.at(20)
        self.assertGreaterEqual(zenith, 90)
        self.assertEqual((sky.ghi_w_m2, sky.dni_w_m2, sky.dhi_w_m2), (0.0, 0.0, 0.0))

    def test_midday_is_a_plausible_tropical_clear_sky(self):
        _, zenith, sky = self.at(6)
        self.assertLess(zenith, 20)
        self.assertGreater(sky.ghi_w_m2, 850)
        self.assertLess(sky.ghi_w_m2, 1100)
        self.assertGreater(sky.dni_w_m2, 700)

    def test_components_close_against_the_horizontal_projection(self):
        # GHI = DNI*cos(zenith) + DHI. Ineichen models the three separately,
        # so this is a real consistency check - it is not true by construction
        # the way the old fixed-fraction split was.
        for hour in (3, 6, 9, 11):
            with self.subTest(hour=hour):
                _, zenith, sky = self.at(hour)
                if sky.ghi_w_m2 <= 1:
                    continue
                projected = sky.dni_w_m2 * math.cos(math.radians(zenith)) + sky.dhi_w_m2
                self.assertAlmostEqual(projected / sky.ghi_w_m2, 1.0, delta=0.06)

    def test_diffuse_fraction_climbs_as_the_sun_drops(self):
        # The whole reason this module was rewritten. A fixed fraction cannot
        # do this, and the error is largest exactly where it used to be worst.
        fractions = []
        for hour in (6, 8, 10, 11, 12):
            _, zenith, sky = self.at(hour)
            if sky.ghi_w_m2 > 1:
                fractions.append((zenith, sky.dhi_w_m2 / sky.ghi_w_m2))
        fractions.sort()
        shares = [f for _, f in fractions]
        self.assertEqual(shares, sorted(shares))
        self.assertLess(shares[0], 0.25)
        self.assertGreater(shares[-1], 0.30)

    def test_turbidity_climatology_gives_a_hazy_semi_arid_value(self):
        from solar_twin.clear_sky import _linke_turbidity
        turbidity = _linke_turbidity(self.location.latitude_deg, self.location.longitude_deg, 2025, 6)
        self.assertGreater(turbidity, 3.0)
        self.assertLess(turbidity, 7.0)

    def test_rejects_an_impossible_zenith(self):
        when = datetime(2025, 6, 1, 6, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            clear_sky_irradiance(self.location, when, -5)
        with self.assertRaises(ValueError):
            clear_sky_irradiance(self.location, when, 200)

    def test_result_is_the_documented_shape(self):
        _, _, sky = self.at(6)
        self.assertIsInstance(sky, ClearSkyIrradiance)


class DemoSupportTests(unittest.TestCase):
    def test_air_temperature_peaks_in_the_afternoon(self):
        hours = [synthesize_air_temperature_c(h) for h in range(24)]
        self.assertEqual(hours.index(max(hours)), 18)
        self.assertEqual(hours.index(min(hours)), 6)

    def test_air_temperature_stays_in_its_band(self):
        for hour in range(0, 24):
            self.assertGreaterEqual(synthesize_air_temperature_c(hour), 20.0 - 1e-9)
            self.assertLessEqual(synthesize_air_temperature_c(hour), 36.0 + 1e-9)

    def test_rejects_an_hour_outside_the_day(self):
        with self.assertRaises(ValueError):
            synthesize_air_temperature_c(25)

    def test_clear_sky_demo_declares_no_cloud(self):
        self.assertEqual(SYNTHETIC_CLOUD_COVER_FRACTION, 0.0)


if __name__ == "__main__":
    unittest.main()
