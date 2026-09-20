from datetime import datetime, timezone
import unittest

import pvlib

from solar_twin.location import Location
from solar_twin.solar_position import solar_position, solar_position_series

# A synthetic mid-latitude, zero-longitude site keeps solar noon at 12:00 UTC
# and avoids the (correct, but confusing to hand-check) case where the sun
# passes north of zenith near the tropics in summer.
MID_LATITUDE = Location("Test site", latitude_deg=40.0, longitude_deg=0.0,
                         elevation_m=0, timezone="UTC")


class SolarPositionTests(unittest.TestCase):
    def test_local_midnight_is_below_horizon(self):
        position = solar_position(MID_LATITUDE, datetime(2010, 12, 21, 0, 0, tzinfo=timezone.utc))
        self.assertGreater(position.zenith_deg, 90)
        self.assertIn("sun_below_horizon", position.warnings)

    def test_winter_solstice_solar_noon_matches_latitude_minus_declination(self):
        # Declination at the Dec solstice is approximately -23.44 deg; with
        # latitude 40N and longitude 0, solar noon falls at ~12:00 UTC.
        position = solar_position(MID_LATITUDE, datetime(2010, 12, 21, 12, 0, tzinfo=timezone.utc))
        self.assertAlmostEqual(position.zenith_deg, 63.44, delta=0.5)
        self.assertAlmostEqual(position.azimuth_deg, 180, delta=1)
        self.assertEqual(position.warnings, ())

    def test_matches_nrel_published_reference(self):
        # NREL/TP-560-34302 Appendix A.5. SPA *is* the reference implementation,
        # so this should agree to the printed precision once the paper's own
        # atmosphere is used. The tolerance below is for our altitude-derived
        # pressure, which differs slightly from the paper's stated 820 mbar.
        site = Location("NREL reference", 39.742476, -105.1786, 1830.14, "UTC")
        result = solar_position(site, datetime(2003, 10, 17, 19, 30, 30, tzinfo=timezone.utc))
        self.assertAlmostEqual(result.zenith_deg, 50.11162, delta=0.01)
        self.assertAlmostEqual(result.azimuth_deg, 194.34024, delta=0.001)

    def test_reproduces_the_reference_exactly_under_the_papers_atmosphere(self):
        # Pinning the claim above: with the paper's pressure and temperature,
        # agreement is to well under an arcsecond.
        import pandas as pd
        frame = pvlib.solarposition.spa_python(
            pd.DatetimeIndex(["2003-10-17 19:30:30"], tz="UTC"),
            39.742476, -105.1786, altitude=1830.14,
            pressure=82000, temperature=11, delta_t=67)
        self.assertAlmostEqual(float(frame["apparent_zenith"].iloc[0]), 50.11162, places=5)
        self.assertAlmostEqual(float(frame["azimuth"].iloc[0]), 194.34024, places=5)

    def test_reports_the_refracted_zenith(self):
        # Apparent, not geometric: refraction lifts a low sun by roughly half a
        # degree, and it is the apparent position a pyranometer sees.
        when = datetime(2010, 3, 21, 18, 0, tzinfo=timezone.utc)
        frame = solar_position_series(MID_LATITUDE, [when])
        scalar = solar_position(MID_LATITUDE, when)
        self.assertAlmostEqual(scalar.zenith_deg, float(frame["apparent_zenith"].iloc[0]), places=9)
        self.assertLess(frame["apparent_zenith"].iloc[0], frame["zenith"].iloc[0])

    def test_series_matches_the_scalar_call(self):
        stamps = [datetime(2025, 6, 1, h, tzinfo=timezone.utc) for h in (3, 6, 9, 12)]
        frame = solar_position_series(MID_LATITUDE, stamps)
        self.assertEqual(len(frame), 4)
        for n, when in enumerate(stamps):
            self.assertAlmostEqual(solar_position(MID_LATITUDE, when).zenith_deg,
                                   float(frame["apparent_zenith"].iloc[n]), places=9)

    def test_requires_utc_aware_timestamp(self):
        with self.assertRaises(ValueError):
            solar_position(MID_LATITUDE, datetime(2010, 12, 21, 12, 0))
        with self.assertRaises(ValueError):
            solar_position(MID_LATITUDE, "2010-12-21T12:00:00Z")
        with self.assertRaises(ValueError):
            solar_position_series(MID_LATITUDE, [])


if __name__ == "__main__":
    unittest.main()
