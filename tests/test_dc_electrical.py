from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import unittest

from solar_twin.dc import Conditions
from solar_twin.dc_electrical import evaluate_dc_electrical
from solar_twin.plant import load_plant

ROOT = Path(__file__).resolve().parents[1]


class DCElectricalTests(unittest.TestCase):
    def setUp(self):
        self.plant = load_plant(ROOT / "config/plant.json")
        self.stc = Conditions(1000, 25, 1, cell_temperature_override_c=25)

    def test_stc_recovers_hand_calculated_values_with_no_warnings(self):
        result = evaluate_dc_electrical(self.plant, self.stc)
        self.assertAlmostEqual(result.string_vmp_v, 29 * 37.7)
        self.assertAlmostEqual(result.string_voc_v, 29 * 45.5)
        self.assertAlmostEqual(result.module_operating_current_a, 17.27)
        self.assertAlmostEqual(result.block_operating_current_a, 17.27 * 345)
        self.assertEqual(result.warnings, ())

    def test_extreme_heat_drops_vmp_below_mppt_range(self):
        hot = replace(self.stc, cell_temperature_override_c=85)
        result = evaluate_dc_electrical(self.plant, hot)
        self.assertLess(result.string_vmp_v, self.plant.inverter.nominal_mppt_min_v)
        self.assertIn("string_voltage_below_mppt_range", result.warnings)
        self.assertNotIn("string_voltage_above_mppt_range", result.warnings)
        self.assertNotIn("open_circuit_voltage_exceeds_system_limit", result.warnings)
        self.assertNotIn("operating_current_exceeds_inverter_limit", result.warnings)

    def test_extreme_cold_raises_voc_above_system_limit(self):
        cold = Conditions(1000, -35, 2, cell_temperature_override_c=-35)
        result = evaluate_dc_electrical(self.plant, cold)
        limit = min(self.plant.module.max_system_voltage_v, self.plant.inverter.max_dc_voltage_v)
        self.assertGreater(result.string_voc_v, limit)
        self.assertIn("open_circuit_voltage_exceeds_system_limit", result.warnings)
        self.assertIn("cell_temperature_below_static_design_minimum", result.warnings)
        self.assertNotIn("string_voltage_below_mppt_range", result.warnings)
        self.assertNotIn("operating_current_exceeds_inverter_limit", result.warnings)

    def test_high_irradiance_exceeds_inverter_current_limit(self):
        bright = replace(self.stc, poa_irradiance_w_m2=1100)
        result = evaluate_dc_electrical(self.plant, bright)
        self.assertGreater(result.block_operating_current_a, self.plant.inverter.max_dc_current_a)
        self.assertIn("operating_current_exceeds_inverter_limit", result.warnings)
        self.assertNotIn("string_voltage_below_mppt_range", result.warnings)
        self.assertNotIn("open_circuit_voltage_exceeds_system_limit", result.warnings)

    def test_night_has_zero_voltage_and_current(self):
        night = Conditions(0, 20, 2)
        result = evaluate_dc_electrical(self.plant, night)
        self.assertEqual(result.module_operating_current_a, 0)
        self.assertEqual(result.block_operating_current_a, 0)
        self.assertEqual(result.string_voc_v, 0)
        self.assertEqual(result.string_vmp_v, 0)
        self.assertEqual(result.warnings, ())

    def test_dc_warnings_are_preserved(self):
        result = evaluate_dc_electrical(self.plant, replace(self.stc, cell_temperature_override_c=90))
        self.assertEqual(result.dc.warnings, ("cell_temperature_outside_module_datasheet_range",))
        self.assertIn("cell_temperature_outside_module_datasheet_range", result.warnings)

    def test_cli_runs_examples_and_emits_json(self):
        completed = subprocess.run(
            [sys.executable, "-m", "solar_twin", "--simulate-electrical", "examples/electrical_scenarios.json"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        scenarios = json.loads(completed.stdout)["scenarios"]
        self.assertEqual(len(scenarios), 5)
        self.assertEqual(scenarios[0]["warnings"], [])
        self.assertIn("string_voltage_below_mppt_range", scenarios[1]["warnings"])
        self.assertIn("open_circuit_voltage_exceeds_system_limit", scenarios[2]["warnings"])
        self.assertIn("operating_current_exceeds_inverter_limit", scenarios[3]["warnings"])
        self.assertEqual(scenarios[4]["warnings"], [])


if __name__ == "__main__":
    unittest.main()
