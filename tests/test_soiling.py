from datetime import datetime, timedelta, timezone
import unittest

from solar_twin.soiling import (
    SoilingParameters, simulate_soiling, soiling_after_dry_days,
)

START = datetime(2025, 1, 1, tzinfo=timezone.utc)
FAST = SoilingParameters(deposition_rate_per_day=0.002)


def hours(count):
    return [START + timedelta(hours=h) for h in range(count)]


class SoilingParameterTests(unittest.TestCase):
    def test_rejects_out_of_range_parameters(self):
        for changes in ({"deposition_rate_per_day": -0.1}, {"cleaning_threshold_mm": 0},
                        {"maximum_soiling_fraction": 1.5}, {"rain_accumulation_hours": 2.5},
                        {"grace_period_days": -1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                SoilingParameters(**changes)


class DrySpellTests(unittest.TestCase):
    def test_linear_accumulation(self):
        self.assertAlmostEqual(soiling_after_dry_days(10, FAST), 0.02)

    def test_capped_at_the_maximum(self):
        self.assertEqual(soiling_after_dry_days(10000, FAST), FAST.maximum_soiling_fraction)


class SoilingSeriesTests(unittest.TestCase):
    def test_dry_month_accumulates_at_the_deposition_rate(self):
        timestamps = hours(24 * 30 + 1)
        series = simulate_soiling(timestamps, [0.0] * len(timestamps), FAST)
        self.assertAlmostEqual(series.final_soiling_loss_fraction, 0.06, places=6)
        self.assertEqual(series.cleaning_events, 0)
        self.assertIn("no_rainfall_reached_the_cleaning_threshold_in_this_window", series.warnings)

    def test_heavy_rain_cleans_the_array(self):
        timestamps = hours(24 * 40)
        rain = [0.0] * len(timestamps)
        rain[24 * 20] = 8.0
        series = simulate_soiling(timestamps, rain, FAST)
        self.assertGreater(series.points[24 * 20 - 1].soiling_loss_fraction, 0.03)
        self.assertEqual(series.points[24 * 20].soiling_loss_fraction, 0.0)
        self.assertTrue(series.points[24 * 20].cleaned)

    def test_light_rain_below_the_threshold_does_not_clean(self):
        timestamps = hours(24 * 20)
        rain = [0.0] * len(timestamps)
        rain[24 * 10] = 2.0
        series = simulate_soiling(timestamps, rain, FAST)
        self.assertEqual(series.cleaning_events, 0)
        self.assertGreater(series.points[24 * 10].soiling_loss_fraction, 0)

    def test_rain_accumulates_across_the_window(self):
        # Three 2.5 mm hours inside 24 h clear the 6 mm threshold together.
        timestamps = hours(48)
        rain = [0.0] * 48
        for hour in (10, 16, 22):
            rain[hour] = 2.5
        series = simulate_soiling(timestamps, rain, FAST)
        self.assertEqual(series.cleaning_events, 1)
        self.assertTrue(series.points[22].cleaned)
        self.assertFalse(series.points[16].cleaned)

    def test_one_storm_counts_once_not_once_per_remembered_hour(self):
        timestamps = hours(24 * 10)
        rain = [0.0] * len(timestamps)
        rain[24 * 2] = 10.0
        rain[24 * 6] = 10.0
        self.assertEqual(simulate_soiling(timestamps, rain, FAST).cleaning_events, 2)

    def test_grace_period_keeps_the_array_clean(self):
        timestamps = hours(24 * 40)
        rain = [0.0] * len(timestamps)
        rain[24 * 5] = 10.0
        series = simulate_soiling(timestamps, rain, FAST)
        self.assertEqual(series.points[24 * 15].soiling_loss_fraction, 0.0)   # inside grace
        self.assertGreater(series.points[24 * 25].soiling_loss_fraction, 0.0)  # after grace

    def test_state_can_be_carried_in_from_a_previous_run(self):
        timestamps = hours(24 * 5 + 1)
        series = simulate_soiling(timestamps, [0.0] * len(timestamps), FAST,
                                  initial_soiling_fraction=0.05)
        self.assertAlmostEqual(series.final_soiling_loss_fraction, 0.05 + 0.01, places=6)

    def test_maximum_is_reported_as_a_limit_not_hidden(self):
        timestamps = hours(24 * 400)
        series = simulate_soiling(timestamps, [0.0] * len(timestamps), FAST)
        self.assertEqual(series.final_soiling_loss_fraction, FAST.maximum_soiling_fraction)
        self.assertIn("soiling_pinned_at_configured_maximum_longer_dry_spells_are_not_distinguished",
                      series.warnings)

    def test_rejects_malformed_input(self):
        with self.assertRaises(ValueError):
            simulate_soiling([], [], FAST)
        with self.assertRaises(ValueError):
            simulate_soiling(hours(3), [0.0, 0.0], FAST)
        with self.assertRaises(ValueError):
            simulate_soiling([START, START], [0.0, 0.0], FAST)
        with self.assertRaises(ValueError):
            simulate_soiling([datetime(2025, 1, 1)], [0.0], FAST)
        with self.assertRaises(ValueError):
            simulate_soiling(hours(2), [0.0, -1.0], FAST)


if __name__ == "__main__":
    unittest.main()
