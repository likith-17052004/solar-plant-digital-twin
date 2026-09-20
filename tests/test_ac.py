from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from solar_twin.ac import InverterParameters, convert_inverter, simulate_ac
from solar_twin.dc import Conditions
from solar_twin.plant import load_plant

ROOT = Path(__file__).resolve().parents[1]


class ACTests(unittest.TestCase):
    def setUp(self):
        self.plant = load_plant(ROOT / "config/plant.json")
        self.parameters = InverterParameters()
        self.sunny = Conditions(1000, 35, 2)

    def convert(self, dc, temperature=35, parameters=None):
        return convert_inverter(dc, temperature, self.plant.inverter, parameters or self.parameters)

    def test_nominal_point_recovers_nominal_efficiency(self):
        result = self.convert(5 / 0.987)
        self.assertAlmostEqual(result.ac_output_mw, 5)
        self.assertAlmostEqual(result.operating_efficiency, 0.987)
        self.assertAlmostEqual(result.unharvested_dc_mw, 0)

    def test_partial_load_uses_load_dependent_efficiency(self):
        # PVWatts zeta=0.5 reference point, normalized to 5 MW nominal AC.
        result = self.convert(0.5 * 5 / 0.987)
        self.assertAlmostEqual(result.ac_output_mw, 2.5057071702812075)
        self.assertNotAlmostEqual(result.operating_efficiency, 0.987, places=4)

    def test_zero_and_low_light_have_no_negative_ac_or_fictitious_heat(self):
        for dc, status in ((0, "no_dc_power"), (0.001, "low_power_standby")):
            result = self.convert(dc)
            self.assertEqual(result.status, status)
            self.assertEqual(result.ac_output_mw, 0)
            self.assertEqual(result.accepted_dc_mw, 0)
            self.assertEqual(result.conversion_loss_mw, 0)
            self.assertEqual(result.unharvested_dc_mw, dc)
            self.assertIsNone(result.operating_efficiency)

    def test_clipping_does_not_turn_unharvested_solar_power_into_heat(self):
        result = self.convert(7)
        self.assertEqual(result.ac_output_mw, 5)
        self.assertAlmostEqual(result.accepted_dc_mw, 5 / 0.987)
        self.assertGreater(result.unharvested_dc_mw, 1.9)
        self.assertLess(result.conversion_loss_mw, 0.1)
        self.assertGreater(result.nameplate_clipping_ac_mw, 0)
        self.assertEqual(result.temperature_reduction_ac_mw, 0)

    def test_thermal_curve_boundaries_and_shutdown(self):
        for temperature, ac in ((-35, 5), (49.99, 5), (50, 5), (55, 4.5), (60, 4),
                                (60.01, 0), (-35.01, 0)):
            with self.subTest(temperature=temperature):
                self.assertAlmostEqual(self.convert(7, temperature).ac_output_mw, ac)
        self.assertEqual(self.convert(7, 61).status, "temperature_shutdown")

    def test_temperature_capacity_reduction_is_not_always_an_output_loss(self):
        normal = self.convert(1, 35)
        hot = self.convert(1, 55)
        self.assertEqual(hot.ac_output_mw, normal.ac_output_mw)
        self.assertLess(hot.temperature_capacity_factor, 1)
        self.assertEqual(hot.temperature_reduction_ac_mw, 0)

    def test_power_budgets_and_limits_across_operating_envelope(self):
        for temperature in (-40, 25, 50, 52.5, 55, 60, 65):
            previous = 0
            for dc in (0, 0.001, 0.03, 0.1, 1, 2.5, 5, 6.5, 10, 15):
                with self.subTest(temperature=temperature, dc=dc):
                    r = self.convert(dc, temperature)
                    self.assertAlmostEqual(dc, r.accepted_dc_mw + r.unharvested_dc_mw)
                    self.assertAlmostEqual(r.accepted_dc_mw, r.ac_output_mw + r.conversion_loss_mw)
                    self.assertGreaterEqual(r.conversion_loss_mw, 0)
                    self.assertGreaterEqual(r.unharvested_dc_mw, 0)
                    self.assertLessEqual(r.ac_output_mw, r.temperature_ac_limit_mw)
                    self.assertGreaterEqual(r.ac_output_mw, previous)
                    if r.operating_efficiency is not None:
                        self.assertLessEqual(r.operating_efficiency, 0.99 + 1e-12)
                    previous = r.ac_output_mw

    def test_panel_temperature_does_not_directly_derate_inverter(self):
        result = simulate_ac(self.plant, self.sunny)
        self.assertGreater(result.dc.cell_temperature_c, 60)
        self.assertEqual(result.inverter_air_temperature_c, 35)
        self.assertEqual(result.plant_inverter_ac_mw, 100)
        self.assertEqual(result.plant_temperature_reduction_ac_mw, 0)

    def test_inlet_temperature_override_leaves_panel_dc_unchanged(self):
        baseline = simulate_ac(self.plant, self.sunny)
        hot = simulate_ac(self.plant, self.sunny, inverter_air_temperature_c=55)
        self.assertEqual(baseline.dc, hot.dc)
        self.assertAlmostEqual(hot.plant_inverter_ac_mw, 90)
        self.assertAlmostEqual(hot.plant_temperature_reduction_ac_mw, 10)
        self.assertIn("assumed_inverter_temperature_limit_active", hot.warnings)

    def test_aggregation_and_ids_match_the_plant(self):
        result = simulate_ac(self.plant, self.sunny)
        self.assertEqual(len(result.blocks), 20)
        self.assertEqual([b.inverter_id for b in result.blocks],
                         [b["inverter_id"] for b in self.plant.topology()["blocks"]])
        self.assertAlmostEqual(result.dc.plant_available_dc_mw,
                               result.plant_inverter_ac_mw + result.plant_conversion_loss_mw + result.plant_unharvested_dc_mw)
        smaller = replace(self.plant, layout=replace(self.plant.layout, blocks=2, export_limit_mw=10))
        self.assertAlmostEqual(simulate_ac(smaller, self.sunny).plant_inverter_ac_mw, 10)

    def test_terminal_ac_does_not_pretend_to_enforce_grid_export(self):
        limited_grid = replace(self.plant, layout=replace(self.plant.layout, export_limit_mw=80))
        result = simulate_ac(limited_grid, self.sunny)
        self.assertEqual(result.plant_inverter_ac_mw, 100)
        self.assertIn("not net grid export", result.scope)
        self.assertIn("dc_voltage_current_and_mppt_feasibility_not_modeled", result.assumptions)

    def test_input_and_parameter_validation(self):
        for value in (-1, True, "5", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.convert(value)
        for value in (True, float("nan"), 80):
            with self.subTest(temperature=value), self.assertRaises(ValueError):
                self.convert(5, value)
        for changes in ({"nominal_efficiency": 1.1}, {"maximum_efficiency": 0.95},
                        {"derating_start_air_temperature_c": 60}, {"derating_fraction_per_c": -1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                InverterParameters(**changes)

    def test_custom_derating_curve(self):
        parameters = replace(self.parameters, derating_fraction_per_c=0.04)
        self.assertAlmostEqual(self.convert(7, 55, parameters).ac_output_mw, 4)

    def test_cli_runs_ac_scenarios_with_explicit_model(self):
        completed = subprocess.run(
            [sys.executable, "-m", "solar_twin", "--simulate-ac", "examples/ac_scenarios.json",
             "--inverter-model", "config/inverter_model.json"],
            cwd=ROOT, text=True, capture_output=True, check=True,
        )
        scenarios = json.loads(completed.stdout)["scenarios"]
        self.assertEqual(len(scenarios), 7)
        self.assertEqual(scenarios[0]["plant_inverter_ac_mw"], 0)
        self.assertEqual(scenarios[3]["plant_inverter_ac_mw"], 100)
        self.assertEqual(scenarios[-1]["plant_inverter_ac_mw"], 0)
        self.assertEqual(scenarios[-2]["inverter_air_temperature_source"], "provided")

    def test_cli_rejects_model_errors_and_incompatible_modes(self):
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "model.json"
            model.write_text('{"nominal_efficiency": 2}', encoding="utf-8")
            for args in (["--simulate-ac", "examples/ac_scenarios.json", "--inverter-model", str(model)],
                         ["--simulate", "examples/dc_scenarios.json", "--inverter-model", str(model)]):
                completed = subprocess.run([sys.executable, "-m", "solar_twin", *args],
                                           cwd=ROOT, text=True, capture_output=True)
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(completed.stdout, "")
                self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
