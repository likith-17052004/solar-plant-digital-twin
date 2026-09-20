from datetime import datetime, timezone
import unittest

from solar_twin.location import Location
from solar_twin.solar_position import solar_position

# A synthetic mid-latitude, zero-longitude site keeps solar noon at 12:00 UTC
# and avoids the (correct, but confusing to hand-check) case where the sun
# passes north of zenith near the tropics in summer.
MID_LATITUDE = Location("Test site", latitude_deg=40.0, longitude_deg=0.0,
                         elevation_m=0, timezone="UTC")


class SolarPositionTests(unittest.TestCase):
    def test_local_midnight_is_below_horizon(self):
        position = solar_position(MID_LATITUDE, datetime(2010, 12, 21, 0, 0, tzinfo=timezone.utc))
        self.assertGreater(position.zenith_deg, 90)

    def test_winter_solstice_solar_noon_matches_latitude_minus_declination(self):
        # Declination at the Dec solstice is approximately -23.44 deg; with
        # latitude 40N and longitude 0, solar noon falls at ~12:00 UTC.
        position = solar_position(MID_LATITUDE, datetime(2010, 12, 21, 12, 0, tzinfo=timezone.utc))
        self.assertAlmostEqual(position.zenith_deg, 63.44, delta=0.5)
        self.assertAlmostEqual(position.azimuth_deg, 180, delta=1)
        self.assertEqual(position.warnings, ())

    def test_date_outside_psa_validity_window_is_flagged(self):
        position = solar_position(MID_LATITUDE, datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc))
        self.assertIn("date_outside_psa_validity_window", position.warnings)

    def test_matches_nrel_published_reference(self):
        # NREL/TP-560-34302, Appendix A.5: https://docs.nlr.gov/docs/fy08osti/34302.pdf
        # PSA omits atmospheric refraction, so allow 0.05 degrees vs SPA.
        site = Location("NREL reference", 39.742476, -105.1786, 1830.14, "UTC")
        result = solar_position(site, datetime(2003, 10, 17, 19, 30, 30, tzinfo=timezone.utc))
        self.assertAlmostEqual(result.zenith_deg, 50.11162, delta=0.05)
        self.assertAlmostEqual(result.azimuth_deg, 194.34024, delta=0.05)

    def test_requires_utc_aware_timestamp(self):
        with self.assertRaises(ValueError):
            solar_position(MID_LATITUDE, datetime(2010, 12, 21, 12, 0))
        with self.assertRaises(ValueError):
            solar_position(MID_LATITUDE, "2010-12-21T12:00:00Z")


if __name__ == "__main__":
    unittest.main()
