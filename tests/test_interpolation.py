from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from solar_twin.clear_sky import estimate_clear_sky_ghi, synthesize_dni_dhi
from solar_twin.interpolation import interpolate_weather, interpolation_warnings
from solar_twin.plant import load_plant
from solar_twin.solar_position import solar_position
from solar_twin.weather import WeatherObservation

CONFIG = Path(__file__).resolve().parents[1] / "config" / "plant.json"
MORNING = datetime(2025, 10, 5, 3, 0, tzinfo=timezone.utc)   # 08:30 IST


def series(count=3, **overrides):
    rows = []
    for n in range(count):
        rows.append(WeatherObservation(
            timestamp_utc=MORNING + timedelta(hours=n),
            dni_w_m2=overrides.get("dni", [500, 300, 600])[n % 3],
            dhi_w_m2=overrides.get("dhi", [110, 220, 120])[n % 3],
            ghi_w_m2=overrides.get("ghi", [420, 300, 500])[n % 3],
            air_temperature_c=27.0 + n,
            wind_speed_10m_m_s=2.0 + n * 0.4,
            cloud_cover_percent=[20.0, 80.0, 30.0][n % 3],
            precipitation_mm=[0.0, 1.2, 0.0][n % 3],
        ))
    return rows


class InterpolationTests(unittest.TestCase):
    def setUp(self):
        self.location = load_plant(CONFIG).location

    def test_sample_count_and_span(self):
        source = series(3)
        out = interpolate_weather(self.location, source, 4)
        # Two intervals of four, plus the closing sample.
        self.assertEqual(len(out), 9)
        self.assertEqual(out[0].timestamp_utc, source[0].timestamp_utc)
        self.assertEqual(out[-1].timestamp_utc, source[-1].timestamp_utc)

    def test_steps_are_evenly_spaced(self):
        out = interpolate_weather(self.location, series(3), 4)
        gaps = {(out[i].timestamp_utc - out[i - 1].timestamp_utc).total_seconds()
                for i in range(1, len(out))}
        self.assertEqual(gaps, {900.0})

    def test_measured_samples_pass_through_untouched(self):
        source = series(4)
        out = {o.timestamp_utc: o for o in interpolate_weather(self.location, source, 4)}
        for original in source:
            with self.subTest(at=original.timestamp_utc):
                produced = out[original.timestamp_utc]
                for field in ("ghi_w_m2", "dni_w_m2", "dhi_w_m2",
                              "air_temperature_c", "wind_speed_10m_m_s", "cloud_cover_percent"):
                    self.assertEqual(getattr(produced, field), getattr(original, field))

    def test_cloud_cover_moves_every_step(self):
        # The whole point: playback at 15 minutes must not hold one value for
        # four frames and then jump.
        out = interpolate_weather(self.location, series(3), 4)
        covers = [o.cloud_cover_percent for o in out]
        self.assertEqual(covers, [20, 35, 50, 65, 80, 67.5, 55, 42.5, 30])
        self.assertEqual(len(set(covers[:5])), 5)

    def test_temperature_and_wind_interpolate_linearly(self):
        out = interpolate_weather(self.location, series(2), 4)
        self.assertAlmostEqual(out[2].air_temperature_c, 27.5, places=9)
        self.assertAlmostEqual(out[2].wind_speed_10m_m_s, 2.2, places=9)

    def test_rainfall_total_is_preserved_not_smeared(self):
        source = series(3)
        out = interpolate_weather(self.location, source, 4)
        # The last source sample closes the series and carries its own value,
        # so compare against the intervals that were actually subdivided.
        self.assertAlmostEqual(sum(o.precipitation_mm for o in out),
                               sum(o.precipitation_mm for o in source[:-1]) + source[-1].precipitation_mm,
                               places=9)

    def test_irradiance_follows_the_sun_not_a_straight_line(self):
        # Hold the atmosphere constant at both ends: the interpolated
        # irradiance must then trace the sun's own curve through the hour and
        # keep exactly that transmission at every step. Interpolating GHI
        # directly would instead draw a chord across it.
        transmission = 0.7
        ends = []
        for n in (0, 1):
            when = MORNING + timedelta(hours=n)
            zenith = solar_position(self.location, when).zenith_deg
            split = synthesize_dni_dhi(estimate_clear_sky_ghi(zenith), zenith)
            ends.append(WeatherObservation(
                when, transmission * split.dni_w_m2, transmission * split.dhi_w_m2,
                transmission * split.ghi_w_m2, 27, 2, 20, 0))
        out = interpolate_weather(self.location, ends, 4)

        values = [o.ghi_w_m2 for o in out]
        self.assertEqual(values, sorted(values))
        for sample in out:
            zenith = solar_position(self.location, sample.timestamp_utc).zenith_deg
            self.assertAlmostEqual(sample.ghi_w_m2 / estimate_clear_sky_ghi(zenith),
                                   transmission, places=6)
        # Distinct from the chord a direct interpolation would have drawn.
        chord_midpoint = (values[0] + values[-1]) / 2
        self.assertGreater(abs(values[2] - chord_midpoint), 1.0)

    def test_night_stays_dark(self):
        midnight = datetime(2025, 10, 5, 19, 0, tzinfo=timezone.utc)
        dark = [WeatherObservation(midnight + timedelta(hours=n), 0, 0, 0, 22, 1, 40, 0)
                for n in range(3)]
        out = interpolate_weather(self.location, dark, 4)
        self.assertTrue(all(o.ghi_w_m2 == 0 and o.dni_w_m2 == 0 and o.dhi_w_m2 == 0 for o in out))

    def test_values_stay_within_physical_bounds(self):
        # Construction alone would raise if any field left its valid range.
        out = interpolate_weather(self.location, series(6), 4)
        for sample in out:
            self.assertGreaterEqual(sample.ghi_w_m2, 0)
            self.assertLessEqual(sample.ghi_w_m2, 1500)
            self.assertGreaterEqual(sample.cloud_cover_percent, 0)
            self.assertLessEqual(sample.cloud_cover_percent, 100)

    def test_one_step_per_hour_returns_the_original_series(self):
        source = series(3)
        self.assertEqual(interpolate_weather(self.location, source, 1), source)

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            interpolate_weather(self.location, series(1), 4)
        with self.assertRaises(ValueError):
            interpolate_weather(self.location, series(3), 0)
        with self.assertRaises(ValueError):
            interpolate_weather(self.location, series(3), 2.5)
        reversed_series = list(reversed(series(3)))
        with self.assertRaisesRegex(ValueError, "increasing"):
            interpolate_weather(self.location, reversed_series, 4)

    def test_warnings_declare_the_interpolation(self):
        self.assertEqual(interpolation_warnings(1), ())
        notes = interpolation_warnings(4)
        self.assertIn("sub_hourly_values_are_interpolated_from_hourly_observations_not_measured", notes)
        self.assertTrue(any("adds_resolution_not_information" in note for note in notes))


if __name__ == "__main__":
    unittest.main()


class WeatherDayResamplingTests(unittest.TestCase):
    """The server route that hands the resampled series to the frontend.

    Network-free: `Session.weather_day` is fed a cached series directly, so
    this exercises the resampling and labelling without calling Open-Meteo.
    """

    def setUp(self):
        from solar_twin.server import Session
        self.plant = load_plant(CONFIG)
        self.session = Session(self.plant)
        hourly = [
            WeatherObservation(
                timestamp_utc=MORNING + timedelta(hours=n),
                dni_w_m2=[500, 300, 600, 640][n], dhi_w_m2=[110, 220, 120, 115][n],
                ghi_w_m2=[420, 300, 500, 540][n], air_temperature_c=27.0 + n,
                wind_speed_10m_m_s=2.0, cloud_cover_percent=[20.0, 80.0, 30.0, 25.0][n],
                precipitation_mm=0.0)
            for n in range(4)
        ]
        self.session._weather["2025-10-05"] = hourly
        self.session._soiling["2025-10-05"] = (0.03, "test_fixture", ())

    def response(self, steps):
        from solar_twin.server import weather_day_response
        return weather_day_response(self.session, "2025-10-05", steps)

    def test_hourly_request_is_unchanged(self):
        body = self.response(1)
        self.assertEqual(len(body), 4)
        self.assertTrue(all(row["measured"] for row in body))
        self.assertTrue(all(row["weather_source"] == "open_meteo" for row in body))
        self.assertTrue(all(row["weather_warnings"] == [] for row in body))

    def test_quarter_hourly_request_fills_the_gaps(self):
        body = self.response(4)
        self.assertEqual(len(body), 13)
        self.assertEqual(sum(1 for row in body if row["measured"]), 4)

    def test_interpolated_rows_are_labelled_and_carry_their_warnings(self):
        interpolated = [row for row in self.response(4) if not row["measured"]]
        self.assertEqual(len(interpolated), 9)
        for row in interpolated:
            self.assertEqual(row["weather_source"], "open_meteo_interpolated")
            self.assertIn("sub_hourly_values_are_interpolated_from_hourly_observations_not_measured",
                          row["weather_warnings"])

    def test_cloud_cover_changes_at_every_step(self):
        # The behaviour this whole module exists for.
        covers = [round(row["cloud_cover_fraction"], 6) for row in self.response(4)]
        self.assertEqual(len(set(covers[:5])), 5)
        self.assertEqual(covers[0], 0.20)
        self.assertEqual(covers[4], 0.80)

    def test_export_moves_between_hours_instead_of_stepping(self):
        exports = [row["grid"]["net_export_mw"] for row in self.response(4)]
        self.assertEqual(len(set(round(v, 6) for v in exports[:5])), 5)

    def test_rejects_an_unsupported_step(self):
        with self.assertRaisesRegex(ValueError, "steps"):
            self.response(3)
