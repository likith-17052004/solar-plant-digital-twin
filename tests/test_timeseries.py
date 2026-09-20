from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from unittest import mock
import unittest

from solar_twin import __main__ as cli
from solar_twin.plant import load_plant
from solar_twin.timeseries import simulate_weather_series, snapshot_to_dict
from solar_twin.weather import WeatherObservation

ROOT = Path(__file__).resolve().parents[1]
CONFIG = str(ROOT / "config/plant.json")

NIGHT = WeatherObservation(datetime(2024, 6, 1, 0, 0, tzinfo=timezone.utc), 0, 0, 0, 25, 2, 20, 0)
DAY = WeatherObservation(datetime(2024, 6, 1, 6, 0, tzinfo=timezone.utc), 700, 150, 750, 35, 2, 20, 0)

MOCK_PAYLOAD = json.dumps({
    "hourly": {
        "time": ["2024-06-01T00:00", "2024-06-01T06:00"],
        "direct_normal_irradiance": [0.0, 700.0],
        "diffuse_radiation": [0.0, 150.0],
        "shortwave_radiation": [0.0, 750.0],
        "temperature_2m": [25.0, 35.0],
        "wind_speed_10m": [2.0, 2.0],
        "cloud_cover": [10.0, 20.0],
        "precipitation": [0.0, 0.0],
    }
}).encode()


class TimeseriesTests(unittest.TestCase):
    def setUp(self):
        self.plant = load_plant(CONFIG)

    def test_night_observation_yields_zero_dc(self):
        [snapshot] = simulate_weather_series(self.plant, [NIGHT])
        self.assertEqual(snapshot.result.plant_available_dc_mw, 0)
        self.assertGreaterEqual(snapshot.solar_position.zenith_deg, 90)

    def test_ac_flag_runs_inverter_model(self):
        [snapshot] = simulate_weather_series(self.plant, [DAY], ac=True)
        self.assertTrue(hasattr(snapshot.result, "plant_inverter_ac_mw"))
        self.assertGreater(snapshot.result.plant_inverter_ac_mw, 0)

    def test_snapshot_to_dict_is_json_serializable(self):
        [snapshot] = simulate_weather_series(self.plant, [DAY])
        json.dumps(snapshot_to_dict(snapshot))

    @mock.patch("solar_twin.weather.urllib.request.urlopen")
    def test_cli_simulate_weather_outputs_expected_shape(self, urlopen):
        response = mock.MagicMock()
        response.read.return_value = MOCK_PAYLOAD
        response.__enter__.return_value = response
        urlopen.return_value = response

        argv = ["solar_twin", "--config", CONFIG, "--simulate-weather", "2024-06-01", "2024-06-01"]
        with mock.patch("sys.argv", argv), redirect_stdout(io.StringIO()) as out:
            cli.main()
        output = json.loads(out.getvalue())
        self.assertEqual(len(output["scenarios"]), 2)
        self.assertEqual(output["scenarios"][0]["result"]["plant_available_dc_mw"], 0)
        self.assertGreater(output["scenarios"][1]["result"]["plant_available_dc_mw"], 0)

    @mock.patch("solar_twin.weather.urllib.request.urlopen")
    def test_cli_simulate_weather_ac_uses_inverter_model(self, urlopen):
        response = mock.MagicMock()
        response.read.return_value = MOCK_PAYLOAD
        response.__enter__.return_value = response
        urlopen.return_value = response

        argv = ["solar_twin", "--config", CONFIG, "--simulate-weather", "2024-06-01", "2024-06-01", "--ac"]
        with mock.patch("sys.argv", argv), redirect_stdout(io.StringIO()) as out:
            cli.main()
        output = json.loads(out.getvalue())
        self.assertIn("plant_inverter_ac_mw", output["scenarios"][1]["result"])


if __name__ == "__main__":
    unittest.main()
