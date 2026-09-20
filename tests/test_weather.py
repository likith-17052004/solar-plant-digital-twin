import io
import json
from unittest import mock
import unittest
import urllib.error

from solar_twin.location import Location
from solar_twin.weather import fetch_hourly_weather

SITE = Location("Test site", latitude_deg=14.1, longitude_deg=77.28, elevation_m=700, timezone="Asia/Kolkata")

VALID_PAYLOAD = {
    "hourly": {
        "time": ["2024-06-01T00:00", "2024-06-01T01:00"],
        "direct_normal_irradiance": [0.0, 54.6],
        "diffuse_radiation": [0.0, 12.0],
        "shortwave_radiation": [0.0, 17.0],
        "temperature_2m": [25.1, 26.5],
        "wind_speed_10m": [4.40, 3.85],
        "cloud_cover": [10.0, 45.0],
        "precipitation": [0.0, 1.2],
    }
}


def _mock_response(body: bytes):
    response = mock.MagicMock()
    response.read.return_value = body
    response.__enter__.return_value = response
    return response


class WeatherTests(unittest.TestCase):
    @mock.patch("solar_twin.weather.urllib.request.urlopen")
    def test_parses_valid_response(self, urlopen):
        urlopen.return_value = _mock_response(json.dumps(VALID_PAYLOAD).encode())
        observations = fetch_hourly_weather(SITE, "2024-06-01", "2024-06-01")
        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[1].dni_w_m2, 54.6)
        self.assertEqual(observations[1].dhi_w_m2, 12.0)
        self.assertEqual(observations[1].ghi_w_m2, 17.0)
        self.assertEqual(observations[1].air_temperature_c, 26.5)
        self.assertEqual(observations[1].wind_speed_10m_m_s, 3.85)
        self.assertEqual(observations[1].cloud_cover_percent, 45.0)
        self.assertEqual(observations[1].precipitation_mm, 1.2)
        self.assertEqual(observations[0].timestamp_utc.isoformat(), "2024-06-01T00:00:00+00:00")
        # Location and dates must reach the query string.
        requested_url = urlopen.call_args[0][0]
        self.assertIn("latitude=14.1", requested_url)
        self.assertIn("start_date=2024-06-01", requested_url)

    @mock.patch("solar_twin.weather.urllib.request.urlopen")
    def test_http_error_raises_value_error(self, urlopen):
        error = urllib.error.HTTPError("url", 500, "Server Error", {}, io.BytesIO())
        urlopen.side_effect = error
        try:
            with self.assertRaisesRegex(ValueError, "HTTP 500"):
                fetch_hourly_weather(SITE, "2024-06-01", "2024-06-01")
        finally:
            error.close()

    @mock.patch("solar_twin.weather.urllib.request.urlopen")
    def test_connection_failure_raises_os_error(self, urlopen):
        urlopen.side_effect = urllib.error.URLError("Connection refused")
        with self.assertRaises(OSError):
            fetch_hourly_weather(SITE, "2024-06-01", "2024-06-01")

    @mock.patch("solar_twin.weather.urllib.request.urlopen")
    def test_malformed_json_raises_value_error(self, urlopen):
        urlopen.return_value = _mock_response(b"not json")
        with self.assertRaises(ValueError):
            fetch_hourly_weather(SITE, "2024-06-01", "2024-06-01")

    @mock.patch("solar_twin.weather.urllib.request.urlopen")
    def test_missing_field_raises_value_error(self, urlopen):
        broken = json.loads(json.dumps(VALID_PAYLOAD))
        del broken["hourly"]["diffuse_radiation"]
        urlopen.return_value = _mock_response(json.dumps(broken).encode())
        with self.assertRaises(ValueError):
            fetch_hourly_weather(SITE, "2024-06-01", "2024-06-01")

    @mock.patch("solar_twin.weather.urllib.request.urlopen")
    def test_mismatched_lengths_raise_value_error(self, urlopen):
        broken = json.loads(json.dumps(VALID_PAYLOAD))
        broken["hourly"]["wind_speed_10m"] = [4.40]
        urlopen.return_value = _mock_response(json.dumps(broken).encode())
        with self.assertRaises(ValueError):
            fetch_hourly_weather(SITE, "2024-06-01", "2024-06-01")

    @mock.patch("solar_twin.weather.urllib.request.urlopen")
    def test_null_value_raises_value_error(self, urlopen):
        broken = json.loads(json.dumps(VALID_PAYLOAD))
        broken["hourly"]["temperature_2m"][1] = None
        urlopen.return_value = _mock_response(json.dumps(broken).encode())
        with self.assertRaises(ValueError):
            fetch_hourly_weather(SITE, "2024-06-01", "2024-06-01")

    @mock.patch("solar_twin.weather._request_json")
    def test_rejects_malformed_weather_values(self, request):
        for field, value in (("temperature_2m", [True, 20]), ("cloud_cover", [101, 0]),
                             ("shortwave_radiation", [-1, 0]), ("wind_speed_10m", [float("nan"), 0]),
                             ("time", None), ("time", []), ("time", [42, 43]),
                             ("time", ["2024-06-01T01:00", "2024-06-01T00:00"])):
            broken = json.loads(json.dumps(VALID_PAYLOAD))
            broken["hourly"][field] = value
            request.return_value = broken
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                fetch_hourly_weather(SITE, "2024-06-01", "2024-06-01")

    @mock.patch("solar_twin.weather._request_json")
    def test_timestamp_offset_is_converted_to_utc(self, request):
        payload = json.loads(json.dumps(VALID_PAYLOAD))
        payload["hourly"]["time"] = ["2024-06-01T05:30+05:30", "2024-06-01T06:30+05:30"]
        request.return_value = payload
        result = fetch_hourly_weather(SITE, "2024-06-01", "2024-06-01")
        self.assertEqual(result[0].timestamp_utc.isoformat(), "2024-06-01T00:00:00+00:00")

    @mock.patch("solar_twin.weather.urllib.request.urlopen")
    def test_invalid_date_range_never_calls_network(self, urlopen):
        with self.assertRaises(ValueError):
            fetch_hourly_weather(SITE, "2024-06-02", "2024-06-01")
        with self.assertRaises(ValueError):
            fetch_hourly_weather(SITE, "not-a-date", "2024-06-01")
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
