from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import unittest

from solar_twin.dc import Conditions
from solar_twin.grid import GridLossParameters, simulate_grid_export
from solar_twin.plant import load_plant

ROOT = Path(__file__).resolve().parents[1]


class GridTests(unittest.TestCase):
    def setUp(self):
        self.plant = load_plant(ROOT / "config/plant.json")
        self.sunny = Conditions(1000, 35, 2)

    def test_nameplate_ac_matches_hand_calculated_loss_chain(self):
        result = simulate_grid_export(self.plant, self.sunny)
        p, l = GridLossParameters(), self.plant.layout
        self.assertAlmostEqual(result.ac.plant_inverter_ac_mw, 100)

        block_input = 100 / l.blocks
        block_loading = block_input / l.block_transformer_mva
        block_loss = (p.block_transformer_no_load_loss_fraction * l.block_transformer_mva
                     + p.block_transformer_load_loss_fraction * l.block_transformer_mva * block_loading ** 2)
        block_output = (block_input - block_loss) * l.blocks
        self.assertAlmostEqual(result.block_transformer_output_mw, block_output)
        self.assertAlmostEqual(result.block_transformer_loss_mw, block_loss * l.blocks)

        collector_loss = block_output * p.collector_cable_loss_fraction
        collector_bus = block_output - collector_loss
        self.assertAlmostEqual(result.collector_cable_loss_mw, collector_loss)

        grid_loading = collector_bus / l.grid_transformer_mva
        grid_loss = (p.grid_transformer_no_load_loss_fraction * l.grid_transformer_mva
                    + p.grid_transformer_load_loss_fraction * l.grid_transformer_mva * grid_loading ** 2)
        grid_output = collector_bus - grid_loss
        self.assertAlmostEqual(result.grid_transformer_output_mw, grid_output)
        self.assertAlmostEqual(result.grid_transformer_loss_mw, grid_loss)

        hv_loss = grid_output * p.hv_cable_loss_fraction
        poi = grid_output - hv_loss
        self.assertAlmostEqual(result.hv_cable_loss_mw, hv_loss)

        aux = (p.auxiliary_load_per_block_kw * l.blocks + p.plant_auxiliary_load_kw) / 1000
        expected_net = poi - aux
        self.assertAlmostEqual(result.auxiliary_load_mw, aux)
        self.assertAlmostEqual(result.net_export_before_curtailment_mw, expected_net)
        self.assertAlmostEqual(result.net_export_mw, expected_net)
        self.assertEqual(result.curtailment_mw, 0)
        self.assertEqual(result.warnings, ())
        # Net export must be meaningfully below inverter-terminal AC once losses apply.
        self.assertLess(result.net_export_mw, result.ac.plant_inverter_ac_mw)

    def test_night_has_no_losses_or_standby_draw(self):
        result = simulate_grid_export(self.plant, Conditions(0, 25, 2))
        self.assertEqual(result.ac.plant_inverter_ac_mw, 0)
        for field in ("block_transformer_output_mw", "block_transformer_loss_mw", "collector_cable_loss_mw",
                      "grid_transformer_output_mw", "grid_transformer_loss_mw", "hv_cable_loss_mw",
                      "auxiliary_load_mw", "net_export_before_curtailment_mw", "net_export_mw", "curtailment_mw"):
            self.assertEqual(getattr(result, field), 0, field)
        self.assertEqual(result.warnings, ())

    def test_export_limit_curtails_net_export(self):
        constrained = replace(self.plant, layout=replace(self.plant.layout, export_limit_mw=50))
        result = simulate_grid_export(constrained, self.sunny)
        self.assertEqual(result.net_export_mw, 50)
        self.assertGreater(result.curtailment_mw, 0)
        self.assertIn("export_curtailed_at_point_of_interconnection", result.warnings)

    def test_very_low_output_floors_transformer_losses_without_going_negative(self):
        tiny = Conditions(5, 25, 2, cell_temperature_override_c=25)
        result = simulate_grid_export(self.plant, tiny)
        self.assertGreater(result.ac.plant_inverter_ac_mw, 0)
        self.assertEqual(result.block_transformer_output_mw, 0)
        self.assertEqual(result.net_export_mw, 0)
        self.assertIn("block_transformer_no_load_loss_exceeds_input", result.warnings)
        self.assertIn("grid_transformer_no_load_loss_exceeds_input", result.warnings)

    def test_power_budget_conserves_energy_through_sunrise(self):
        for irradiance in (0, 1, 5, 7, 10, 15, 50, 250, 1000):
            with self.subTest(irradiance=irradiance):
                result = simulate_grid_export(self.plant, Conditions(irradiance, 25, 2))
                accounted = sum(getattr(result, field) for field in (
                    "block_transformer_loss_mw", "collector_cable_loss_mw",
                    "grid_transformer_loss_mw", "hv_cable_loss_mw", "auxiliary_load_mw",
                    "net_export_mw", "curtailment_mw"))
                self.assertAlmostEqual(accounted, result.ac.plant_inverter_ac_mw)

    def test_partial_load_has_a_larger_relative_loss_share_than_near_nameplate(self):
        cloudy = simulate_grid_export(self.plant, Conditions(250, 30, 2))
        sunny = simulate_grid_export(self.plant, self.sunny)
        cloudy_loss_fraction = 1 - cloudy.net_export_mw / cloudy.ac.plant_inverter_ac_mw
        sunny_loss_fraction = 1 - sunny.net_export_mw / sunny.ac.plant_inverter_ac_mw
        self.assertGreater(cloudy_loss_fraction, sunny_loss_fraction)

    def test_ac_warnings_are_preserved(self):
        hot_inverter = simulate_grid_export(self.plant, self.sunny, inverter_air_temperature_c=61)
        self.assertEqual(hot_inverter.ac.blocks[0].inverter.status, "temperature_shutdown")
        self.assertIn("assumed_inverter_temperature_limit_active", hot_inverter.warnings)

    def test_rejects_invalid_grid_loss_parameters(self):
        for changes in ({"collector_cable_loss_fraction": -0.01}, {"collector_cable_loss_fraction": 0.2},
                        {"auxiliary_load_per_block_kw": float("nan")},
                        {"block_transformer_no_load_loss_fraction": 0.6, "block_transformer_load_loss_fraction": 0.6},
                        {"grid_transformer_no_load_loss_fraction": 0.6, "grid_transformer_load_loss_fraction": 0.6}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                GridLossParameters(**changes)

    def test_cli_runs_examples_and_emits_json(self):
        completed = subprocess.run(
            [sys.executable, "-m", "solar_twin", "--simulate-grid", "examples/grid_scenarios.json"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        scenarios = json.loads(completed.stdout)["scenarios"]
        self.assertEqual(len(scenarios), 3)
        self.assertEqual(scenarios[0]["net_export_mw"], 0)
        self.assertGreater(scenarios[2]["net_export_mw"], scenarios[1]["net_export_mw"])

    def test_cli_grid_loss_model_override(self):
        completed = subprocess.run(
            [sys.executable, "-m", "solar_twin", "--simulate-grid", "examples/grid_scenarios.json",
             "--grid-loss-model", "config/grid_loss_model.json"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        self.assertEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
