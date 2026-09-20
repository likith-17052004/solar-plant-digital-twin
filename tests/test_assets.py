from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from solar_twin.assets import (
    INVERTER_DERATED, INVERTER_OFFLINE, LOCALISED_SOILING, STRINGS_DISCONNECTED,
    BlockFault, BlockState, PlantState, degradation_loss_fraction, nominal_state,
    with_faults, with_soiling,
)
from solar_twin.plant import load_plant

CONFIG = Path(__file__).resolve().parents[1] / "config" / "plant.json"
NOW = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
BUILT = datetime(2021, 6, 1, tzinfo=timezone.utc)


def block(**changes):
    defaults = dict(block_id="BLK-001", inverter_id="INV-001", commissioned_utc=BUILT)
    return BlockState(**{**defaults, **changes})


class BlockFaultTests(unittest.TestCase):
    def test_rejects_unknown_kind(self):
        with self.assertRaisesRegex(ValueError, "Unknown fault kind"):
            BlockFault("gremlins", NOW)

    def test_offline_is_all_or_nothing(self):
        with self.assertRaisesRegex(ValueError, "all or nothing"):
            BlockFault(INVERTER_OFFLINE, NOW, severity=0.5)

    def test_requires_utc_and_valid_severity(self):
        with self.assertRaises(ValueError):
            BlockFault(INVERTER_DERATED, datetime(2025, 6, 1), severity=0.5)
        with self.assertRaises(ValueError):
            BlockFault(INVERTER_DERATED, NOW, severity=1.5)

    def test_every_kind_has_a_description(self):
        for kind in (INVERTER_OFFLINE, INVERTER_DERATED, STRINGS_DISCONNECTED, LOCALISED_SOILING):
            self.assertTrue(BlockFault(kind, NOW, severity=1.0 if kind == INVERTER_OFFLINE else 0.2).description)


class BlockStateTests(unittest.TestCase):
    def test_healthy_block_derates_nothing(self):
        healthy = block(soiling_loss_fraction=0.04)
        self.assertTrue(healthy.healthy)
        self.assertTrue(healthy.inverter_available)
        self.assertEqual(healthy.string_availability, 1.0)
        self.assertEqual(healthy.inverter_capacity_fraction, 1.0)
        self.assertEqual(healthy.effective_soiling_loss_fraction, 0.04)

    def test_offline_inverter_removes_all_capacity_but_not_the_array(self):
        faulted = block(fault=BlockFault(INVERTER_OFFLINE, NOW))
        self.assertFalse(faulted.inverter_available)
        self.assertEqual(faulted.inverter_capacity_fraction, 0.0)
        self.assertEqual(faulted.string_availability, 1.0)

    def test_string_outage_removes_its_share(self):
        faulted = block(fault=BlockFault(STRINGS_DISCONNECTED, NOW, severity=0.25))
        self.assertEqual(faulted.string_availability, 0.75)
        self.assertEqual(faulted.inverter_capacity_fraction, 1.0)

    def test_derating_reduces_inverter_capacity_only(self):
        faulted = block(fault=BlockFault(INVERTER_DERATED, NOW, severity=0.3))
        self.assertAlmostEqual(faulted.inverter_capacity_fraction, 0.7)
        self.assertEqual(faulted.string_availability, 1.0)
        self.assertTrue(faulted.inverter_available)

    def test_localised_soiling_adds_to_the_fleet_level(self):
        faulted = block(soiling_loss_fraction=0.03, fault=BlockFault(LOCALISED_SOILING, NOW, severity=0.06))
        self.assertAlmostEqual(faulted.effective_soiling_loss_fraction, 0.09)

    def test_localised_soiling_cannot_exceed_total_opacity(self):
        faulted = block(soiling_loss_fraction=0.85, fault=BlockFault(LOCALISED_SOILING, NOW, severity=0.5))
        self.assertLessEqual(faulted.effective_soiling_loss_fraction, 0.9)

    def test_age(self):
        self.assertAlmostEqual(block().age_years(NOW), 4.0, places=2)
        self.assertEqual(block(commissioned_utc=NOW).age_years(NOW), 0.0)
        self.assertEqual(block(commissioned_utc=NOW + timedelta(days=30)).age_years(NOW), 0.0)

    def test_rejects_malformed_state(self):
        with self.assertRaises(ValueError):
            block(block_id="  ")
        with self.assertRaises(ValueError):
            block(commissioned_utc=datetime(2021, 6, 1))
        with self.assertRaises(ValueError):
            block(soiling_loss_fraction=1.5)
        with self.assertRaises(ValueError):
            block(fault="broken")


class DegradationTests(unittest.TestCase):
    def test_first_year_step_then_linear(self):
        self.assertEqual(degradation_loss_fraction(0, 0.01, 0.004), 0.0)
        self.assertAlmostEqual(degradation_loss_fraction(0.5, 0.01, 0.004), 0.005)
        self.assertAlmostEqual(degradation_loss_fraction(1, 0.01, 0.004), 0.01)
        self.assertAlmostEqual(degradation_loss_fraction(25, 0.01, 0.004), 0.106)

    def test_monotonic(self):
        values = [degradation_loss_fraction(a, 0.01, 0.004) for a in (0, 1, 5, 10, 25, 40)]
        self.assertEqual(values, sorted(values))

    def test_plant_exposes_the_warranty_curve(self):
        plant = load_plant(CONFIG)
        self.assertAlmostEqual(plant.degradation_loss_fraction(25), 0.106)
        self.assertAlmostEqual(plant.summary()["warranty_retention_at_25_years"], 0.894)


class PlantStateTests(unittest.TestCase):
    def setUp(self):
        self.plant = load_plant(CONFIG)
        self.state = nominal_state(self.plant, NOW, BUILT, soiling_loss_fraction=0.02)

    def test_nominal_state_covers_every_block_and_matches_topology(self):
        self.assertEqual(len(self.state.blocks), self.plant.layout.blocks)
        self.assertEqual([b.block_id for b in self.state.blocks],
                         [b["id"] for b in self.plant.topology()["blocks"]])
        self.assertEqual(self.state.faulted, ())
        self.assertTrue(all(b.soiling_loss_fraction == 0.02 for b in self.state.blocks))

    def test_rejects_commissioning_in_the_future(self):
        with self.assertRaisesRegex(ValueError, "commissioned_utc"):
            nominal_state(self.plant, BUILT, NOW)

    def test_rejects_duplicate_or_empty_blocks(self):
        with self.assertRaisesRegex(ValueError, "at least one block"):
            PlantState(NOW, (), "test")
        with self.assertRaisesRegex(ValueError, "unique"):
            PlantState(NOW, (block(), block()), "test")

    def test_faults_apply_only_to_named_blocks(self):
        faulted = with_faults(self.state, {"BLK-007": BlockFault(INVERTER_OFFLINE, NOW)},
                              source="fault_injection")
        self.assertEqual([b.block_id for b in faulted.faulted], ["BLK-007"])
        self.assertEqual(faulted.source, "fault_injection")
        self.assertTrue(faulted.block("BLK-001").healthy)
        self.assertFalse(faulted.block("BLK-007").inverter_available)
        # The original state is untouched.
        self.assertEqual(self.state.faulted, ())

    def test_unknown_block_id_is_an_error_not_a_silent_no_op(self):
        with self.assertRaises(KeyError):
            with_faults(self.state, {"BLK-999": BlockFault(INVERTER_OFFLINE, NOW)})
        with self.assertRaises(KeyError):
            self.state.block("BLK-999")

    def test_soiling_updates_leave_faults_alone(self):
        faulted = with_faults(self.state, {"BLK-003": BlockFault(STRINGS_DISCONNECTED, NOW, 0.5)})
        resoiled = with_soiling(faulted, 0.07)
        self.assertEqual(resoiled.block("BLK-003").fault.kind, STRINGS_DISCONNECTED)
        self.assertTrue(all(b.soiling_loss_fraction == 0.07 for b in resoiled.blocks))


if __name__ == "__main__":
    unittest.main()
