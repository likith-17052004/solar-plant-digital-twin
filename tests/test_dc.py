from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from solar_twin.dc import Conditions, ThermalParameters, estimate_cell_temperature, simulate_dc
from solar_twin.plant import load_plant

ROOT = Path(__file__).resolve().parents[1]


class DCTests(unittest.TestCase):
    def setUp(self):
        self.plant = load_plant(ROOT / "config/plant.json")
        self.stc = Conditions(1000, 25, 1, cell_temperature_override_c=25)

    def test_stc_recovers_nameplate_and_block_totals(self):
        result = simulate_dc(self.plant, self.stc)
        self.assertEqual(result.module_available_dc_w, 650)
        self.assertAlmostEqual(result.plant_available_dc_mw, 130.065)
        self.assertAlmostEqual(sum(b.available_dc_mw for b in result.blocks), result.plant_available_dc_mw)
        self.assertEqual([b.block_id for b in result.blocks], [b["id"] for b in self.plant.topology()["blocks"]])
        # DC potential must not be silently capped at the AC export limit.
        self.assertGreater(result.plant_available_dc_mw, self.plant.layout.export_limit_mw)

    def test_published_sandia_temperature_example(self):
        # pvlib sapm_cell docs: POA=1000, air=10, wind=0, open-rack glass/glass.
        self.assertAlmostEqual(estimate_cell_temperature(Conditions(1000, 10, 0)), 44.11703066106086)

    def test_zero_irradiance_means_zero_power(self):
        for air in (-20, 0, 45):
            result = simulate_dc(self.plant, Conditions(0, air, 2))
            self.assertEqual(result.plant_available_dc_mw, 0)
            self.assertEqual(result.cell_temperature_c, air)

    def test_temperature_coefficient_at_fixed_irradiance(self):
        hot = simulate_dc(self.plant, replace(self.stc, cell_temperature_override_c=50))
        self.assertAlmostEqual(hot.module_available_dc_w, 594.75)
        cold = simulate_dc(self.plant, replace(self.stc, cell_temperature_override_c=0))
        self.assertAlmostEqual(cold.module_available_dc_w, 705.25)

    def test_low_light_stays_linear_at_fixed_cell_temperature(self):
        for irradiance in (0.001, 50, 125, 250, 1200):
            with self.subTest(irradiance=irradiance):
                result = simulate_dc(self.plant, replace(self.stc, poa_irradiance_w_m2=irradiance))
                self.assertAlmostEqual(result.module_available_dc_w, 0.65 * irradiance)

    def test_heat_reduces_output_and_wind_cools_cells(self):
        weather = Conditions(1000, 35, 2)
        baseline = simulate_dc(self.plant, weather)
        hot = simulate_dc(self.plant, replace(weather, air_temperature_c=45))
        windy = simulate_dc(self.plant, replace(weather, wind_speed_10m_m_s=6))
        self.assertGreater(baseline.cell_temperature_c, weather.air_temperature_c)
        self.assertLess(hot.plant_available_dc_mw, baseline.plant_available_dc_mw)
        self.assertLess(windy.cell_temperature_c, baseline.cell_temperature_c)
        self.assertGreater(windy.plant_available_dc_mw, baseline.plant_available_dc_mw)

    def test_soiling_reduces_electrical_input_once(self):
        clean = simulate_dc(self.plant, Conditions(1000, 35, 2))
        dirty = simulate_dc(self.plant, replace(clean.conditions, soiling_loss_fraction=0.05))
        self.assertEqual(clean.cell_temperature_c, dirty.cell_temperature_c)
        self.assertAlmostEqual(dirty.plant_available_dc_mw, clean.plant_available_dc_mw * 0.95)
        opaque = simulate_dc(self.plant, replace(clean.conditions, soiling_loss_fraction=1))
        self.assertEqual(opaque.plant_available_dc_mw, 0)

    def test_cell_temperature_override_bypasses_thermal_estimation(self):
        result = simulate_dc(self.plant, replace(self.stc, air_temperature_c=50, wind_speed_10m_m_s=0))
        self.assertEqual(result.cell_temperature_c, 25)
        self.assertEqual(result.cell_temperature_source, "provided")

    def test_extreme_temperatures_are_flagged_without_faking_inverter_behavior(self):
        cold = simulate_dc(self.plant, replace(self.stc, cell_temperature_override_c=-20))
        self.assertIn("cell_temperature_below_static_design_minimum", cold.warnings)
        hot = simulate_dc(self.plant, replace(self.stc, cell_temperature_override_c=90))
        self.assertIn("cell_temperature_outside_module_datasheet_range", hot.warnings)
        self.assertEqual(simulate_dc(self.plant, self.stc).warnings, ())

    def test_rejects_bad_weather_and_losses(self):
        for field in ("poa_irradiance_w_m2", "air_temperature_c", "wind_speed_10m_m_s",
                      "soiling_loss_fraction", "cell_temperature_override_c"):
            for value in (True, "25", float("nan"), float("inf")):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    replace(self.stc, **{field: value})
        for changes in ({"poa_irradiance_w_m2": -1}, {"wind_speed_10m_m_s": -1},
                        {"soiling_loss_fraction": 1.01}, {"air_temperature_c": 100},
                        {"cell_temperature_override_c": -273}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.stc, **changes)

    def test_custom_thermal_parameters_are_validated(self):
        for changes in ({"a": float("nan")}, {"b": 0.1}, {"cell_module_delta_at_1000w_c": -1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ThermalParameters(**changes)

    def test_cli_runs_examples_and_emits_json(self):
        completed = subprocess.run(
            [sys.executable, "-m", "solar_twin", "--simulate", "examples/dc_scenarios.json"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        scenarios = json.loads(completed.stdout)["scenarios"]
        self.assertEqual(len(scenarios), 7)
        self.assertAlmostEqual(scenarios[0]["plant_available_dc_mw"], 130.065)
        self.assertEqual(scenarios[1]["plant_available_dc_mw"], 0)

    def test_cli_rejects_invalid_scenarios_without_partial_results(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "invalid.json"
            for payload in ({}, [], [{"name": "bad", "conditions": {"poa_irradiance_w_m2": -1,
                                "air_temperature_c": 25, "wind_speed_10m_m_s": 2}}]):
                path.write_text(json.dumps(payload), encoding="utf-8")
                completed = subprocess.run(
                    [sys.executable, "-m", "solar_twin", "--simulate", str(path)],
                    cwd=ROOT, capture_output=True, text=True,
                )
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(completed.stdout, "")
                self.assertIn("Invalid input", completed.stderr)


if __name__ == "__main__":
    unittest.main()
