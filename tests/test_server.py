from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from solar_twin.plant import load_plant
from solar_twin.server import make_handler

ROOT = Path(__file__).resolve().parents[1]


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        plant = load_plant(ROOT / "config/plant.json")
        handler = make_handler(plant, Path(cls.folder.name) / "history.sqlite3")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.folder.cleanup()

    def setUp(self):
        self._post("/api/scenario/faults", {})   # each test starts from a healthy plant

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=20) as response:
            return response.status, json.loads(response.read())

    def _post(self, path, body):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method="POST",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())

    def _expect_400(self, path):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get(path)
        try:
            self.assertEqual(ctx.exception.code, 400)
        finally:
            ctx.exception.close()

    # -- plant ----------------------------------------------------------

    def test_plant_endpoint_exposes_geometry_and_topology(self):
        status, body = self._get("/api/plant")
        self.assertEqual(status, 200)
        self.assertEqual(body["layout"]["blocks"], 20)
        self.assertEqual(body["location"]["timezone"], "Asia/Kolkata")
        self.assertAlmostEqual(body["geometry"]["ground_coverage_ratio"], 2.384 / 6.8, places=6)
        self.assertEqual(len(body["topology"]["blocks"]), 20)
        self.assertTrue(body["scenario"]["fault_kinds"])

    # -- snapshot -------------------------------------------------------

    def test_snapshot_at_local_noon_is_producing(self):
        status, body = self._get("/api/snapshot?date=2025-06-01&hour=12")
        self.assertEqual(status, 200)
        self.assertEqual(body["weather_source"], "synthetic_clear_sky")
        self.assertLess(body["solar_position"]["zenith_deg"], 90)
        self.assertGreater(body["electrical"]["dc"]["plant_available_dc_mw"], 0)
        self.assertGreater(body["grid"]["net_export_mw"], 0)
        self.assertEqual(len(body["fleet"]["blocks"]), 20)
        self.assertEqual(body["fleet"]["waterfall"][0]["step"], "nameplate_dc_at_incident_poa")

    def test_snapshot_at_local_midnight_is_zero(self):
        status, body = self._get("/api/snapshot?date=2025-06-01&hour=0")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["solar_position"]["zenith_deg"], 90)
        self.assertEqual(body["electrical"]["dc"]["plant_available_dc_mw"], 0)
        self.assertEqual(body["grid"]["net_export_mw"], 0)

    def test_healthy_snapshot_has_no_flags_and_matches_expectation(self):
        _, body = self._get("/api/snapshot?date=2025-06-01&hour=10")
        self.assertEqual(body["residuals"]["flagged_block_ids"], [])
        self.assertEqual(body["fleet"]["faulted_block_ids"], [])
        self.assertAlmostEqual(body["grid"]["net_export_mw"],
                               body["grid"]["expected_net_export_mw"], places=9)

    def test_snapshot_says_where_its_soiling_figure_came_from(self):
        _, body = self._get("/api/snapshot?date=2025-06-01&hour=10")
        self.assertIn("demo_default", body["state"]["soiling_source"])
        self.assertIn("soiling_is_a_stated_demo_default_not_derived_from_rainfall_load_weather_for_the_real_value",
                      body["state"]["warnings"])
        self.assertGreater(body["state"]["soiling_loss_fraction"], 0)

    def test_snapshot_rejects_bad_input(self):
        self._expect_400("/api/snapshot?date=2025-06-01&hour=25")
        self._expect_400("/api/snapshot?date=2025-06-01")
        self._expect_400("/api/snapshot?date=not-a-date&hour=12")

    # -- scenario -------------------------------------------------------

    def test_injected_fault_shows_up_in_the_next_snapshot(self):
        status, scenario = self._post("/api/scenario/faults", {
            "BLK-007": {"kind": "inverter_offline", "note": "tripped"}})
        self.assertEqual(status, 200)
        self.assertIn("BLK-007", scenario["faults"])

        _, body = self._get("/api/snapshot?date=2025-06-01&hour=10")
        self.assertEqual(body["fleet"]["faulted_block_ids"], ["BLK-007"])
        self.assertIn("BLK-007", body["residuals"]["flagged_block_ids"])
        self.assertLess(body["grid"]["net_export_mw"], body["grid"]["expected_net_export_mw"])
        offline = next(b for b in body["fleet"]["blocks"] if b["block_id"] == "BLK-007")
        self.assertEqual(offline["inverter_ac_mw"], 0)
        self.assertGreater(offline["array_dc_mw"], 0)
        self.assertEqual(offline["flag"], "not_producing")

    def test_clearing_faults_restores_the_healthy_plant(self):
        self._post("/api/scenario/faults", {"BLK-007": {"kind": "inverter_offline"}})
        _, scenario = self._post("/api/scenario/faults", {})
        self.assertEqual(scenario["faults"], {})
        _, body = self._get("/api/snapshot?date=2025-06-01&hour=10")
        self.assertEqual(body["fleet"]["faulted_block_ids"], [])

    def test_scenario_endpoint_reflects_posted_faults(self):
        self._post("/api/scenario/faults", {"BLK-003": {"kind": "strings_disconnected", "severity": 0.4}})
        _, body = self._get("/api/scenario")
        self.assertEqual(body["faults"]["BLK-003"]["severity"], 0.4)
        self.assertTrue(body["faults"]["BLK-003"]["description"])

    def test_bad_fault_requests_are_rejected(self):
        for body in ({"BLK-999": {"kind": "inverter_offline"}},
                     {"BLK-001": {"kind": "gremlins"}},
                     {"BLK-001": {"kind": "inverter_offline", "severity": 0.5}},
                     {"BLK-001": "offline"}):
            with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post("/api/scenario/faults", body)
            try:
                self.assertEqual(ctx.exception.code, 400)
            finally:
                ctx.exception.close()

    # -- weather resampling ----------------------------------------------

    def test_weather_day_rejects_an_unsupported_step(self):
        self._expect_400("/api/weather-day?date=2025-10-05&steps=3")

    # -- day and history -------------------------------------------------

    def test_day_endpoint_returns_a_curve_and_energy_metrics(self):
        status, body = self._get("/api/day?date=2025-06-01&steps=2")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["series"]), 49)
        self.assertEqual(body["series"][0]["local_hour"], 0.0)
        self.assertEqual(body["series"][-1]["local_hour"], 24.0)
        self.assertGreater(body["energy"]["export_energy_mwh"], 0)
        self.assertGreater(body["energy"]["performance_ratio"], 0.5)
        self.assertLess(body["energy"]["performance_ratio"], 1.0)
        self.assertEqual(body["energy"]["warnings"], [])

    def test_day_endpoint_reports_energy_lost_to_faults(self):
        self._post("/api/scenario/faults", {"BLK-007": {"kind": "inverter_offline"}})
        _, body = self._get("/api/day?date=2025-06-01")
        self.assertGreater(body["energy_shortfall_mwh"], 0)
        self.assertLess(body["energy"]["performance_ratio"],
                        body["expected_energy"]["performance_ratio"])

    def test_day_endpoint_rejects_an_unsupported_step(self):
        self._expect_400("/api/day?date=2025-06-01&steps=3")

    def test_recorded_days_appear_in_history(self):
        self._post("/api/scenario/faults", {"BLK-007": {"kind": "inverter_offline"}})
        _, day = self._get("/api/day?date=2025-05-20&record=1")
        self.assertTrue(day["recorded"])
        _, history = self._get("/api/history")
        self.assertTrue(history["enabled"])
        self.assertIn("2025-05-20", [row["local_date"] for row in history["daily"]])
        self.assertIn("BLK-007", [trend["block_id"] for trend in history["worst_blocks"]])

    # -- static ----------------------------------------------------------

    def test_static_index_is_served(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("<title>Solar plant digital twin</title>", response.read().decode("utf-8"))

    def test_unknown_post_endpoint_is_not_found(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/nope", {})
        try:
            self.assertEqual(ctx.exception.code, 404)
        finally:
            ctx.exception.close()


if __name__ == "__main__":
    unittest.main()
