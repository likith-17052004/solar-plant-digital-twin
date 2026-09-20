from datetime import datetime, timezone
import math
from pathlib import Path
import unittest

from solar_twin.array_geometry import apply_array_losses
from solar_twin.assets import (
    INVERTER_DERATED, INVERTER_OFFLINE, LOCALISED_SOILING, STRINGS_DISCONNECTED,
    BlockFault, nominal_state, with_faults,
)
from solar_twin.dc import Conditions
from solar_twin.fleet import simulate_fleet
from solar_twin.irradiance import transpose_to_poa
from solar_twin.plant import load_plant
from solar_twin.solar_position import solar_position

CONFIG = Path(__file__).resolve().parents[1] / "config" / "plant.json"
NOON = datetime(2025, 3, 15, 6, 30, tzinfo=timezone.utc)      # 12:00 IST
# 18:05 IST in late January: the sun is 2.6 degrees up (apparent, after
# refraction) and setting WSW, the
# only time of day this array (GCR 0.35) shades itself at all.
SUNSET = datetime(2025, 1, 28, 12, 35, tzinfo=timezone.utc)
BUILT = datetime(2021, 6, 1, tzinfo=timezone.utc)


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.plant = load_plant(CONFIG)

    def scene(self, when=NOON, dni=820, dhi=130, ghi=900, air=34.0):
        position = solar_position(self.plant.location, when)
        poa = transpose_to_poa(dni, dhi, ghi, position.zenith_deg, position.azimuth_deg,
                               self.plant.layout.tilt_deg, self.plant.layout.azimuth_deg,
                               self.plant.location.ground_albedo)
        plane = apply_array_losses(poa, position.zenith_deg, position.azimuth_deg,
                                   self.plant.row_geometry)
        return plane, Conditions(plane.effective_poa_global_w_m2, air, 2.5)

    def run_fleet(self, faults=None, when=NOON, soiling=0.03, **scene):
        plane, conditions = self.scene(when=when, **scene)
        state = nominal_state(self.plant, when, BUILT, soiling_loss_fraction=soiling)
        if faults:
            state = with_faults(state, faults, source="fault_injection")
        return simulate_fleet(self.plant, conditions, plane, state, when)

    def test_healthy_fleet_blocks_are_identical(self):
        result = self.run_fleet()
        outputs = {round(b.inverter_ac_mw, 9) for b in result.blocks}
        self.assertEqual(len(outputs), 1)
        self.assertEqual(result.faulted_block_ids, ())
        self.assertEqual(len(result.blocks), self.plant.layout.blocks)

    def test_waterfall_closes_exactly(self):
        for faults in (None, {"BLK-007": BlockFault(INVERTER_OFFLINE, NOON),
                              "BLK-012": BlockFault(STRINGS_DISCONNECTED, NOON, 0.25),
                              "BLK-019": BlockFault(LOCALISED_SOILING, NOON, 0.06),
                              "BLK-004": BlockFault(INVERTER_DERATED, NOON, 0.30)}):
            with self.subTest(faults=bool(faults)):
                result = self.run_fleet(faults)
                total = math.fsum(value for _, value in result.loss_waterfall)
                self.assertAlmostEqual(total, result.net_export_mw, places=9)

    def test_waterfall_starts_at_nameplate_and_every_other_step_is_a_loss(self):
        result = self.run_fleet()
        names = [name for name, _ in result.loss_waterfall]
        self.assertEqual(names[0], "nameplate_dc_at_incident_poa")
        self.assertGreater(result.loss_waterfall[0][1], 0)
        for name, value in result.loss_waterfall[1:]:
            with self.subTest(step=name):
                self.assertLessEqual(value, 1e-12)

    def test_offline_inverter_strands_its_array_rather_than_deleting_it(self):
        result = self.run_fleet({"BLK-007": BlockFault(INVERTER_OFFLINE, NOON)})
        block = result.block("BLK-007")
        self.assertGreater(block.array_dc_mw, 0)
        self.assertEqual(block.available_dc_mw, 0.0)
        self.assertEqual(block.inverter_ac_mw, 0.0)
        self.assertAlmostEqual(block.outage_loss_mw, block.array_dc_mw)
        self.assertEqual(block.status, "offline")
        self.assertIn("one_or_more_blocks_are_running_under_a_declared_fault", result.warnings)

    def test_offline_block_still_costs_its_transformers_no_load_loss(self):
        result = self.run_fleet({"BLK-007": BlockFault(INVERTER_OFFLINE, NOON)})
        self.assertGreater(result.cascade.idle_transformer_loss_mw, 0)
        self.assertIn("idle_block_transformers_still_drawing_no_load_losses", result.warnings)

    def test_string_outage_scales_dc_pro_rata(self):
        result = self.run_fleet({"BLK-012": BlockFault(STRINGS_DISCONNECTED, NOON, 0.25)})
        self.assertAlmostEqual(result.block("BLK-012").array_dc_mw,
                               result.block("BLK-001").array_dc_mw * 0.75, places=9)

    def test_derated_inverter_clips_at_its_reduced_limit(self):
        result = self.run_fleet({"BLK-004": BlockFault(INVERTER_DERATED, NOON, 0.30)})
        limit = self.plant.inverter.active_power_limit_mw * 0.7
        self.assertLessEqual(result.block("BLK-004").inverter_ac_mw, limit + 1e-9)
        self.assertGreater(result.block("BLK-004").clipping_loss_mw, 0)

    def test_localised_soiling_only_dirties_its_own_block(self):
        result = self.run_fleet({"BLK-019": BlockFault(LOCALISED_SOILING, NOON, 0.06)})
        self.assertAlmostEqual(result.block("BLK-019").soiling_loss_fraction, 0.09)
        self.assertAlmostEqual(result.block("BLK-001").soiling_loss_fraction, 0.03)
        self.assertLess(result.block("BLK-019").array_dc_mw, result.block("BLK-001").array_dc_mw)

    def test_faults_reduce_export(self):
        healthy = self.run_fleet().net_export_mw
        faulted = self.run_fleet({"BLK-007": BlockFault(INVERTER_OFFLINE, NOON)}).net_export_mw
        self.assertLess(faulted, healthy)

    def test_setting_sun_produces_row_shading(self):
        result = self.run_fleet(when=SUNSET, dni=300, dhi=80, ghi=140)
        self.assertAlmostEqual(result.array_plane.shaded_row_fraction, 0.294, places=2)
        shading = dict(result.loss_waterfall)["row_shading"]
        self.assertLess(shading, 0)
        self.assertIn("beam_partially_blocked_by_row_in_front", result.warnings)

    def test_high_sun_has_no_row_shading(self):
        result = self.run_fleet()
        self.assertEqual(result.array_plane.shaded_row_fraction, 0.0)
        self.assertEqual(dict(result.loss_waterfall)["row_shading"], 0.0)

    def test_night_exports_nothing(self):
        midnight = datetime(2025, 3, 14, 18, 30, tzinfo=timezone.utc)
        result = self.run_fleet(when=midnight, dni=0, dhi=0, ghi=0, air=22.0)
        self.assertEqual(result.net_export_mw, 0.0)
        self.assertEqual(result.plant_inverter_ac_mw, 0.0)

    def test_older_plant_produces_less(self):
        plane, conditions = self.scene()
        young = nominal_state(self.plant, NOON, datetime(2024, 6, 1, tzinfo=timezone.utc))
        old = nominal_state(self.plant, NOON, datetime(2005, 6, 1, tzinfo=timezone.utc))
        self.assertLess(simulate_fleet(self.plant, conditions, plane, old, NOON).available_dc_mw,
                        simulate_fleet(self.plant, conditions, plane, young, NOON).available_dc_mw)

    def test_rejects_double_counted_soiling(self):
        plane, conditions = self.scene()
        state = nominal_state(self.plant, NOON, BUILT)
        from dataclasses import replace
        with self.assertRaisesRegex(ValueError, "soiling is per block"):
            simulate_fleet(self.plant, replace(conditions, soiling_loss_fraction=0.05),
                           plane, state, NOON)

    def test_rejects_state_that_does_not_match_the_plant(self):
        plane, conditions = self.scene()
        from dataclasses import replace
        state = nominal_state(self.plant, NOON, BUILT)
        short = replace(state, blocks=state.blocks[:5])
        with self.assertRaisesRegex(ValueError, "blocks"):
            simulate_fleet(self.plant, conditions, plane, short, NOON)

    def test_plant_totals_are_the_sum_of_the_blocks(self):
        result = self.run_fleet({"BLK-007": BlockFault(INVERTER_OFFLINE, NOON),
                                 "BLK-012": BlockFault(STRINGS_DISCONNECTED, NOON, 0.25)})
        self.assertAlmostEqual(result.plant_inverter_ac_mw,
                               math.fsum(b.inverter_ac_mw for b in result.blocks), places=9)
        self.assertAlmostEqual(result.available_dc_mw,
                               math.fsum(b.available_dc_mw for b in result.blocks), places=9)


if __name__ == "__main__":
    unittest.main()


class ShadingWindowTests(unittest.TestCase):
    """How often this particular array shades itself, over a whole year.

    Recorded as a test because it is a real finding about the site, not a
    tuning knob: at 14 degrees north with a 6.8 m pitch, row shading is
    confined to the last half hour before sunset and the first after sunrise.
    A tighter pitch would change this, and this test would catch it.
    """

    def test_row_shading_is_rare_at_this_pitch(self):
        from datetime import timedelta
        from solar_twin.array_geometry import shaded_fraction
        from solar_twin.solar_position import solar_position_series
        plant = load_plant(CONFIG)
        geometry = plant.row_geometry
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        # Vectorised: a year of half-hourly positions in one pvlib call rather
        # than 17,520 scalar ones, which is ~150x faster for the same answer.
        stamps = [start + timedelta(minutes=30 * step) for step in range(365 * 24 * 2)]
        frame = solar_position_series(plant.location, stamps)
        daylight = shaded = 0
        for zenith, azimuth in zip(frame["apparent_zenith"], frame["azimuth"]):
            if zenith >= 90:
                continue
            daylight += 1
            if shaded_fraction(float(zenith), float(azimuth), geometry) > 0:
                shaded += 1
        self.assertGreater(daylight, 8000)
        self.assertLess(shaded / daylight, 0.03)
        self.assertGreater(shaded, 0)
