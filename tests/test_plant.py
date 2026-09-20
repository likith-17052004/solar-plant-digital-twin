from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from solar_twin.plant import configuration, load_plant

CONFIG = Path(__file__).resolve().parents[1] / "config" / "plant.json"


class PlantTests(unittest.TestCase):
    def setUp(self):
        self.plant = load_plant(CONFIG)

    def test_capacity_and_asset_accounting(self):
        p = self.plant
        self.assertEqual(p.module_count, 200100)
        self.assertAlmostEqual(p.dc_capacity_mwp, 130.065)
        self.assertEqual(p.ac_capacity_mw, 100)
        blocks = p.topology()["blocks"]
        ids = [s for block in blocks for s in block["string_ids"]]
        self.assertEqual(len(set(ids)), 6900)
        self.assertEqual(sum(len(b["string_ids"]) * b["modules_per_string"] for b in blocks), p.module_count)

    def test_cold_voltage(self):
        self.assertAlmostEqual(self.plant.cold_string_voc_v, 1478.0049375)
        with self.assertRaisesRegex(ValueError, "Cold string"):
            replace(self.plant, layout=replace(self.plant.layout, modules_per_string=31))
        with self.assertRaisesRegex(ValueError, "Cold string"):
            replace(self.plant, design_envelope=replace(self.plant.design_envelope, minimum_cell_temperature_c=-20))

    def test_rejects_overcurrent(self):
        with self.assertRaisesRegex(ValueError, "operating current"):
            replace(self.plant, layout=replace(self.plant.layout, strings_per_block=350))
        with self.assertRaisesRegex(ValueError, "short-circuit current"):
            replace(self.plant, inverter=replace(self.plant.inverter, max_short_circuit_current_a=7000))

    def test_rejects_invalid_numeric_input(self):
        for value in (True, 1.5, 0, -1, float("nan"), float("inf"), "20"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.plant, layout=replace(self.plant.layout, blocks=value))

    def test_rejects_export_and_transformer_overload(self):
        for changes in ({"export_limit_mw": 101}, {"block_transformer_mva": 4}, {"grid_transformer_mva": 90}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.plant, layout=replace(self.plant.layout, **changes))

    def test_rejects_nominal_mppt_mismatch(self):
        with self.assertRaisesRegex(ValueError, "MPPT"):
            replace(self.plant, layout=replace(self.plant.layout, modules_per_string=24))

    def test_rejects_invalid_isc_temperature_coefficient(self):
        for value in (-0.001, float("nan"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.plant, module=replace(self.plant.module, isc_temperature_coefficient_per_c=value))

    def test_rejects_invalid_location(self):
        for changes in ({"latitude_deg": 91}, {"longitude_deg": -181}, {"ground_albedo": 1.5},
                        {"timezone": "Not/AZone"}, {"name": "  "}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.plant, location=replace(self.plant.location, **changes))

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "plant.json"
            path.write_text(json.dumps(configuration(self.plant)), encoding="utf-8")
            self.assertEqual(load_plant(path), self.plant)


if __name__ == "__main__":
    unittest.main()
