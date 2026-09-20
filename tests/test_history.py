from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from solar_twin.history import History

NOW = datetime(2025, 6, 20, 12, 0, tzinfo=timezone.utc)


class Totals:
    """Minimal stand-in for energy.EnergyTotals; History only reads these."""

    def __init__(self, export=700.0, pr=0.78):
        self.poa_irradiation_kwh_m2 = 7.4
        self.export_energy_mwh = export
        self.performance_ratio = pr
        self.specific_yield_kwh_per_kwp = export / 130.065
        self.peak_export_mw = 98.0


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.history = History(Path(self.folder.name) / "history.sqlite3")

    def test_schema_is_created_and_reopening_is_safe(self):
        self.assertEqual(self.history.daily_energy(), [])
        reopened = History(self.history.path)
        self.assertEqual(reopened.daily_energy(), [])

    def test_days_round_trip_in_chronological_order(self):
        for day, export in (("2025-06-01", 700.0), ("2025-06-03", 720.0), ("2025-06-02", 710.0)):
            self.history.record_day(day, "open_meteo", Totals(export), 0.03)
        rows = self.history.daily_energy()
        self.assertEqual([row["local_date"] for row in rows],
                         ["2025-06-01", "2025-06-02", "2025-06-03"])
        self.assertEqual(rows[2]["export_energy_mwh"], 720.0)

    def test_rerunning_a_day_overwrites_rather_than_duplicates(self):
        self.history.record_day("2025-06-01", "synthetic_clear_sky", Totals(700.0), 0.03)
        self.history.record_day("2025-06-01", "open_meteo", Totals(640.0), 0.05)
        rows = self.history.daily_energy()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["export_energy_mwh"], 640.0)
        self.assertEqual(rows[0]["weather_source"], "open_meteo")

    def test_undefined_performance_ratio_is_stored_as_null(self):
        self.history.record_day("2025-06-01", "open_meteo", Totals(0.0, None), 0.0)
        self.assertIsNone(self.history.daily_energy()[0]["performance_ratio"])

    def test_limit_returns_the_most_recent_days(self):
        for n in range(1, 11):
            self.history.record_day(f"2025-06-{n:02}", "open_meteo", Totals(700.0 + n), 0.03)
        rows = self.history.daily_energy(limit=3)
        self.assertEqual([row["local_date"] for row in rows],
                         ["2025-06-08", "2025-06-09", "2025-06-10"])


class FaultLogTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.history = History(Path(folder.name) / "history.sqlite3")

    def test_open_and_close_a_fault(self):
        started = NOW - timedelta(days=9)
        self.history.open_fault("BLK-007", "inverter_offline", 1.0, started, "tripped")
        self.assertEqual(len(self.history.open_faults()), 1)
        self.assertEqual(self.history.close_fault("BLK-007", "inverter_offline", NOW), 1)
        self.assertEqual(self.history.open_faults(), [])

    def test_reopening_the_same_fault_does_not_duplicate_it(self):
        started = NOW - timedelta(days=3)
        for _ in range(5):
            self.history.open_fault("BLK-007", "inverter_offline", 1.0, started)
        self.assertEqual(len(self.history.open_faults()), 1)

    def test_fault_age_is_the_number_that_makes_a_flag_urgent(self):
        self.history.open_fault("BLK-007", "inverter_offline", 1.0, NOW - timedelta(days=9))
        self.assertAlmostEqual(self.history.fault_age_days("BLK-007", "inverter_offline", NOW), 9.0, places=6)
        self.assertIsNone(self.history.fault_age_days("BLK-001", "inverter_offline", NOW))

    def test_closing_a_fault_that_is_not_open_changes_nothing(self):
        self.assertEqual(self.history.close_fault("BLK-001", "inverter_offline", NOW), 0)

    def test_requires_utc_aware_timestamps(self):
        with self.assertRaises(ValueError):
            self.history.open_fault("BLK-007", "inverter_offline", 1.0, datetime(2025, 6, 1))


class BlockTrendTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.history = History(Path(folder.name) / "history.sqlite3")
        # BLK-007 flagged the last 4 days running; BLK-012 flagged once, early.
        for n in range(1, 11):
            day = f"2025-06-{n:02}"
            self.history.record_block_day(day, "BLK-001", 35.0)
            self.history.record_block_day(day, "BLK-007", 20.0 if n > 6 else 35.0,
                                          flagged_hours=8.0 if n > 6 else 0.0,
                                          worst_relative_to_peers=-0.42 if n > 6 else None)
            self.history.record_block_day(day, "BLK-012", 33.0,
                                          flagged_hours=6.0 if n == 2 else 0.0,
                                          worst_relative_to_peers=-0.09 if n == 2 else None)

    def test_healthy_block_has_no_flags(self):
        trend = self.history.block_trend("BLK-001")
        self.assertEqual(trend.days, 10)
        self.assertEqual(trend.days_flagged, 0)
        self.assertEqual(trend.consecutive_days_flagged, 0)
        self.assertIsNone(trend.mean_relative_to_peers)

    def test_persistent_fault_shows_as_a_run_of_days(self):
        trend = self.history.block_trend("BLK-007")
        self.assertEqual(trend.days_flagged, 4)
        self.assertEqual(trend.consecutive_days_flagged, 4)
        self.assertAlmostEqual(trend.mean_relative_to_peers, -0.42, places=6)
        self.assertAlmostEqual(trend.total_export_mwh, 35 * 6 + 20 * 4, places=6)

    def test_an_old_one_off_flag_is_not_a_current_run(self):
        trend = self.history.block_trend("BLK-012")
        self.assertEqual(trend.days_flagged, 1)
        self.assertEqual(trend.consecutive_days_flagged, 0)

    def test_unknown_block_returns_an_empty_trend_rather_than_raising(self):
        trend = self.history.block_trend("BLK-999")
        self.assertEqual((trend.days, trend.days_flagged, trend.total_export_mwh), (0, 0, 0.0))

    def test_worst_blocks_ranks_by_persistence(self):
        worst = self.history.worst_blocks()
        self.assertEqual([t.block_id for t in worst], ["BLK-007", "BLK-012"])
        self.assertEqual(worst[0].days_flagged, 4)


if __name__ == "__main__":
    unittest.main()
