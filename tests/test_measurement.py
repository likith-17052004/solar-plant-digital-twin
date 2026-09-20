from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from solar_twin.array_geometry import apply_array_losses
from solar_twin.assets import (
    INVERTER_OFFLINE, LOCALISED_SOILING, STRINGS_DISCONNECTED,
    BlockFault, nominal_state, with_faults,
)
from solar_twin.clear_sky import estimate_clear_sky_ghi, synthesize_dni_dhi
from solar_twin.dc import Conditions
from solar_twin.fleet import simulate_fleet
from solar_twin.irradiance import transpose_to_poa
from solar_twin.measurement import (
    ABOVE_MODEL, NORMAL, NOT_PRODUCING, UNDERPERFORMING,
    MeasurementNoise, compare_to_model, synthesize_measurement,
)
from solar_twin.plant import load_plant
from solar_twin.solar_position import solar_position

CONFIG = Path(__file__).resolve().parents[1] / "config" / "plant.json"
DAY = datetime(2025, 3, 15, tzinfo=timezone.utc)
BUILT = datetime(2021, 6, 1, tzinfo=timezone.utc)
MORNING = DAY + timedelta(hours=3)    # 08:30 IST, well below clipping
NOON = DAY + timedelta(hours=6, minutes=30)
NIGHT = DAY + timedelta(hours=20)

FAULTS = {
    "BLK-007": BlockFault(INVERTER_OFFLINE, DAY, 1.0, "tripped"),
    "BLK-012": BlockFault(STRINGS_DISCONNECTED, DAY, 0.25, "combiner fuse"),
    "BLK-019": BlockFault(LOCALISED_SOILING, DAY, 0.08, "haul road dust"),
}


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.plant = load_plant(CONFIG)

    def fleet(self, when, faults=None):
        position = solar_position(self.plant.location, when)
        split = synthesize_dni_dhi(estimate_clear_sky_ghi(position.zenith_deg), position.zenith_deg)
        poa = transpose_to_poa(split.dni_w_m2, split.dhi_w_m2, split.ghi_w_m2,
                               position.zenith_deg, position.azimuth_deg,
                               self.plant.layout.tilt_deg, self.plant.layout.azimuth_deg,
                               self.plant.location.ground_albedo)
        plane = apply_array_losses(poa, position.zenith_deg, position.azimuth_deg,
                                   self.plant.row_geometry)
        conditions = Conditions(plane.effective_poa_global_w_m2, 32.0, 2.0)
        state = nominal_state(self.plant, when, BUILT, soiling_loss_fraction=0.035)
        if faults:
            state = with_faults(state, faults, source="fault_injection")
        return simulate_fleet(self.plant, conditions, plane, state, when)

    def report(self, when=MORNING, faults=FAULTS, noise=MeasurementNoise(), **kwargs):
        return compare_to_model(synthesize_measurement(self.fleet(when, faults), noise),
                                self.fleet(when), **kwargs)

    def test_noise_is_reproducible_across_processes(self):
        truth = self.fleet(MORNING, FAULTS)
        self.assertEqual(synthesize_measurement(truth), synthesize_measurement(truth))
        self.assertNotEqual(synthesize_measurement(truth, MeasurementNoise(seed=1)),
                            synthesize_measurement(truth, MeasurementNoise(seed=2)))

    def test_measurement_is_never_negative(self):
        readings = synthesize_measurement(self.fleet(MORNING, FAULTS),
                                          MeasurementNoise(relative_sigma=0.4, absolute_sigma_mw=0.5))
        self.assertTrue(all(value >= 0 for value in readings))

    def test_rejects_invalid_noise(self):
        for changes in ({"relative_sigma": -0.1}, {"absolute_sigma_mw": 2}, {"seed": 1.5}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                MeasurementNoise(**changes)

    def test_healthy_plant_flags_nothing(self):
        report = self.report(faults=None)
        self.assertEqual(report.flagged_block_ids, ())
        self.assertTrue(all(b.flag == NORMAL for b in report.blocks))
        self.assertAlmostEqual(report.fleet_median_ratio, 1.0, places=2)

    def test_offline_block_is_flagged_as_not_producing(self):
        report = self.report()
        self.assertEqual(report.block("BLK-007").flag, NOT_PRODUCING)
        self.assertEqual(report.block("BLK-007").measured_ac_mw, 0.0)
        self.assertGreater(report.block("BLK-007").estimated_shortfall_mw, 0)

    def test_a_dark_block_reading_sensor_noise_is_still_not_producing(self):
        # A tripped inverter reports a few kilowatts of noise, not a clean
        # zero. Classifying on an absolute floor would file it as merely
        # "behind", which is a materially different work order.
        model = self.fleet(MORNING)
        measured = [block.inverter_ac_mw for block in model.blocks]
        measured[6] = 0.007
        report = compare_to_model(measured, model)
        self.assertEqual(report.blocks[6].flag, NOT_PRODUCING)
        self.assertGreater(report.blocks[6].estimated_shortfall_mw, 1.0)

    def test_a_genuinely_partial_block_is_not_called_dark(self):
        model = self.fleet(MORNING)
        measured = [block.inverter_ac_mw for block in model.blocks]
        measured[6] *= 0.5
        self.assertEqual(compare_to_model(measured, model).blocks[6].flag, UNDERPERFORMING)

    def test_detector_recovers_the_injected_severity(self):
        # The point of the whole exercise: a 25 percent string outage and an
        # 8 percent soiling patch must read back at about that size.
        report = self.report()
        self.assertAlmostEqual(report.block("BLK-012").relative_to_peers, -0.25, delta=0.03)
        self.assertAlmostEqual(report.block("BLK-019").relative_to_peers, -0.08, delta=0.03)
        self.assertEqual(report.block("BLK-012").flag, UNDERPERFORMING)
        self.assertEqual(report.block("BLK-019").flag, UNDERPERFORMING)

    def test_peer_comparison_survives_a_plant_wide_model_error(self):
        # Every block reading 12 percent low is a model problem, not 20 faults.
        model = self.fleet(MORNING)
        measured = [b.inverter_ac_mw * 0.88 for b in model.blocks]
        report = compare_to_model(measured, model)
        self.assertEqual(report.flagged_block_ids, ())
        self.assertAlmostEqual(report.fleet_median_ratio, 0.88, places=6)
        self.assertIn("fleet_median_is_far_from_the_model_a_plant_wide_input_or_model_error_is_likely",
                      report.warnings)

    def test_one_bad_block_is_still_found_under_a_plant_wide_error(self):
        model = self.fleet(MORNING)
        measured = [b.inverter_ac_mw * 0.88 for b in model.blocks]
        measured[4] *= 0.7
        report = compare_to_model(measured, model)
        self.assertEqual(report.flagged_block_ids, (model.blocks[4].block_id,))

    def test_clipping_masks_moderate_faults_and_says_so(self):
        report = self.report(when=NOON)
        self.assertIn("model_is_clipping_so_shortfalls_smaller_than_the_clipped_headroom_are_invisible_now",
                      report.warnings)
        self.assertNotIn("BLK-019", report.flagged_block_ids)   # hidden by clipping
        self.assertIn("BLK-007", report.flagged_block_ids)      # a dark block is never hidden

    def test_near_darkness_does_not_flag_noise_as_underperformance(self):
        # At dusk a 5 MW block making a few kW sits within a couple of noise
        # sigma of its neighbours, so peer ratios swing for no physical reason.
        # Flagging there produced six "underperforming" blocks on a clear
        # evening with the plant at 0.1 MW.
        dusk = DAY + timedelta(hours=12, minutes=44)   # sun 3.3 degrees up, ~48 kW a block
        model = self.fleet(dusk)
        producing = [b for b in model.blocks if b.inverter_ac_mw > 0]
        self.assertTrue(producing, "fixture must still be producing a little")
        self.assertLess(max(b.inverter_ac_mw for b in model.blocks), 0.05)
        report = compare_to_model(synthesize_measurement(model), model)
        self.assertEqual(report.flagged_block_ids, ())
        self.assertIn("output_too_low_for_peer_comparison_noise_dominates_at_this_light_level",
                      report.warnings)

    def test_a_real_fault_is_still_caught_in_good_light(self):
        report = self.report()
        self.assertIn("BLK-007", report.flagged_block_ids)

    def test_darkness_produces_no_residuals_and_says_why(self):
        report = self.report(when=NIGHT, faults=None)
        self.assertIsNone(report.fleet_median_ratio)
        self.assertEqual(report.flagged_block_ids, ())
        self.assertIn("no_block_is_modelled_as_producing_residuals_undefined_in_darkness",
                      report.warnings)

    def test_a_block_reading_high_is_flagged_separately_not_ignored(self):
        model = self.fleet(MORNING)
        measured = [b.inverter_ac_mw for b in model.blocks]
        measured[2] *= 1.25
        report = compare_to_model(measured, model)
        self.assertEqual(report.blocks[2].flag, ABOVE_MODEL)
        self.assertEqual(report.flagged_block_ids, ())   # not a shortfall

    def test_plant_totals_and_shortfall_add_up(self):
        report = self.report()
        self.assertAlmostEqual(report.plant_residual_mw,
                               report.measured_plant_ac_mw - report.modelled_plant_ac_mw, places=9)
        self.assertAlmostEqual(report.estimated_total_shortfall_mw,
                               sum(b.estimated_shortfall_mw for b in report.blocks), places=9)

    def test_rejects_mismatched_or_invalid_input(self):
        model = self.fleet(MORNING)
        with self.assertRaisesRegex(ValueError, "Expected"):
            compare_to_model([1.0, 2.0], model)
        with self.assertRaises(ValueError):
            compare_to_model([-1.0] * len(model.blocks), model)
        with self.assertRaises(ValueError):
            compare_to_model([1.0] * len(model.blocks), model, tolerance=0)


if __name__ == "__main__":
    unittest.main()
