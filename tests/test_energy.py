from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from solar_twin.energy import integrate_trapezoid, summarise_energy
from solar_twin.plant import load_plant

CONFIG = Path(__file__).resolve().parents[1] / "config" / "plant.json"
START = datetime(2025, 3, 15, 0, 0, tzinfo=timezone.utc)


def hourly(count):
    return [START + timedelta(hours=h) for h in range(count)]


class IntegrationTests(unittest.TestCase):
    def test_constant_power_over_a_day(self):
        self.assertAlmostEqual(integrate_trapezoid(hourly(25), [2.0] * 25), 48.0)

    def test_triangle_is_exact_under_the_trapezoid_rule(self):
        # A linear ramp is integrated exactly; that is the rule's whole premise.
        values = [float(h) for h in range(11)]
        self.assertAlmostEqual(integrate_trapezoid(hourly(11), values), 50.0)

    def test_rejects_degenerate_input(self):
        with self.assertRaises(ValueError):
            integrate_trapezoid(hourly(2), [1.0])
        with self.assertRaises(ValueError):
            integrate_trapezoid(hourly(1), [1.0])
        with self.assertRaises(ValueError):
            integrate_trapezoid([START, START], [1.0, 1.0])
        with self.assertRaises(ValueError):
            integrate_trapezoid([datetime(2025, 3, 15), datetime(2025, 3, 16)], [1.0, 1.0])


class EnergySummaryTests(unittest.TestCase):
    def setUp(self):
        self.plant = load_plant(CONFIG)

    def summarise(self, hours=25, poa=500.0, export=50.0, **changes):
        timestamps = hourly(hours)
        n = len(timestamps)
        defaults = dict(
            incident_poa_w_m2=[poa] * n, effective_poa_w_m2=[poa] * n,
            available_dc_mw=[export * 1.1] * n, inverter_ac_mw=[export * 1.05] * n,
            net_export_mw=[export] * n, curtailment_mw=[0.0] * n,
        )
        return summarise_energy(self.plant, timestamps, **{**defaults, **changes})

    def test_energy_totals(self):
        totals = self.summarise()
        self.assertAlmostEqual(totals.export_energy_mwh, 50 * 24)
        self.assertAlmostEqual(totals.poa_irradiation_kwh_m2, 500 * 24 / 1000)
        self.assertEqual(totals.peak_export_mw, 50.0)
        self.assertEqual(totals.samples, 25)
        self.assertAlmostEqual(totals.span_hours, 24.0)

    def test_performance_ratio_matches_its_definition(self):
        # PR = E_out / (H_poa * P_dc0 / G_stc), G_stc = 1 kW/m2.
        totals = self.summarise()
        expected = (50 * 24 * 1000) / (12.0 * self.plant.dc_capacity_mwp * 1000)
        self.assertAlmostEqual(totals.performance_ratio, expected, places=9)

    def test_a_perfect_plant_has_performance_ratio_one(self):
        # Export exactly nameplate-scaled with irradiance: PR must be 1.0.
        export = self.plant.dc_capacity_mwp * 0.5   # 500 W/m2 is half of STC
        totals = self.summarise(poa=500.0, export=export)
        self.assertAlmostEqual(totals.performance_ratio, 1.0, places=9)

    def test_specific_yield_and_capacity_factor(self):
        totals = self.summarise()
        self.assertAlmostEqual(totals.specific_yield_kwh_per_kwp,
                               50 * 24 / self.plant.dc_capacity_mwp, places=9)
        self.assertAlmostEqual(totals.capacity_factor, 50 * 24 / (100 * 24), places=9)

    def test_dark_window_leaves_performance_ratio_undefined_rather_than_zero(self):
        totals = self.summarise(poa=0.0, export=0.0)
        self.assertIsNone(totals.performance_ratio)
        self.assertIn("no_plane_of_array_irradiation_in_this_window_performance_ratio_undefined",
                      totals.warnings)

    def test_impossible_performance_ratio_is_flagged_not_hidden(self):
        totals = self.summarise(poa=100.0, export=90.0)
        self.assertGreater(totals.performance_ratio, 1)
        self.assertIn("performance_ratio_above_one_check_the_irradiance_series", totals.warnings)

    def test_coarse_sampling_is_flagged(self):
        timestamps = [START + timedelta(hours=2 * h) for h in range(13)]
        totals = summarise_energy(
            self.plant, timestamps, [500.0] * 13, [500.0] * 13, [55.0] * 13,
            [52.0] * 13, [50.0] * 13, [0.0] * 13)
        self.assertIn(
            "sample_interval_over_thirty_minutes_trapezoid_energy_error_is_largest_at_sunrise_and_sunset",
            totals.warnings)

    def test_partial_window_is_flagged(self):
        self.assertIn("window_is_not_a_whole_day_daily_yield_metrics_are_partial",
                      self.summarise(hours=7).warnings)

    def test_shading_shows_up_as_a_gap_between_incident_and_effective_irradiation(self):
        totals = self.summarise(effective_poa_w_m2=[450.0] * 25)
        self.assertLess(totals.effective_poa_irradiation_kwh_m2, totals.poa_irradiation_kwh_m2)

    def test_rejects_mismatched_series(self):
        with self.assertRaisesRegex(ValueError, "one value per timestamp"):
            self.summarise(net_export_mw=[50.0] * 3)


if __name__ == "__main__":
    unittest.main()
