"""Stdlib-only local server for the digital twin frontend.

Physics stays server-side and reuses every existing module unmodified; the
browser is a thin renderer driven by these JSON responses. No new dependency -
same rule the rest of the backend follows.

The server holds one mutable thing, a `Scenario`: which blocks are faulted,
how dirty the fleet is, and when the plant was commissioned. That is the
twin's declared belief about the physical plant, and it is what makes this a
session rather than a calculator. Every simulation runs twice against it:

* the **model** run, under the healthy nominal state - what the plant should
  be doing;
* the **plant** run, under the scenario's real state - what it is doing.

The difference between them, block by block, is the twin's actual output.
"""

import argparse
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from .array_geometry import apply_array_losses
from .assets import FAULT_DESCRIPTIONS, FAULT_KINDS, BlockFault, nominal_state, with_faults
from .clear_sky import (
    SYNTHETIC_CLOUD_COVER_FRACTION,
    clear_sky_irradiance,
    synthesize_air_temperature_c,
)
from .dc import Conditions
from .dc_electrical import evaluate_dc_electrical
from .energy import summarise_energy
from .fleet import simulate_fleet
from .history import History
from .interpolation import interpolate_weather, interpolation_warnings
from .irradiance import transpose_to_poa
from .measurement import compare_to_model, synthesize_measurement
from .plant import Plant, load_plant
from .soiling import SoilingParameters, simulate_soiling, soiling_after_dry_days
from .solar_position import solar_position
from .weather import fetch_hourly_weather

FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"

# How far back to look for rain when estimating today's soiling. Long enough
# to span a dry spell plus the model's 14 day grace period.
SOILING_LOOKBACK_DAYS = 45

# Commissioning date for the illustrative plant. Sets module age, and so the
# warranty degradation applied to every block. Not a real project milestone.
DEFAULT_COMMISSIONED_UTC = datetime(2021, 6, 1, tzinfo=timezone.utc)

# Fleet soiling when no rainfall history is available (the clear-sky demo
# path). A stated demo default, not derived from any model.
DEFAULT_DRY_DAYS = 20.0


@dataclass
class Scenario:
    """The twin's declared belief about the physical plant."""

    commissioned_utc: datetime = DEFAULT_COMMISSIONED_UTC
    faults: dict[str, BlockFault] = field(default_factory=dict)
    soiling_parameters: SoilingParameters = field(default_factory=SoilingParameters)

    def payload(self) -> dict:
        return {
            "commissioned_utc": self.commissioned_utc.isoformat(),
            "faults": {
                block_id: {"kind": fault.kind, "severity": fault.severity,
                           "since_utc": fault.since_utc.isoformat(), "note": fault.note,
                           "description": fault.description}
                for block_id, fault in self.faults.items()
            },
            "fault_kinds": [{"kind": kind, "description": FAULT_DESCRIPTIONS[kind]} for kind in FAULT_KINDS],
        }


class Session:
    """Server-held state: the scenario, a weather cache, and the history store."""

    def __init__(self, plant: Plant, history_path: str | Path | None = None):
        self.plant = plant
        self.scenario = Scenario()
        self.history = History(history_path) if history_path else None
        self._lock = threading.Lock()
        self._weather: dict[str, list] = {}
        self._soiling: dict[str, tuple[float, str, tuple[str, ...]]] = {}

    # -- weather ---------------------------------------------------------

    def weather_day(self, date_str: str) -> list:
        with self._lock:
            cached = self._weather.get(date_str)
        if cached is not None:
            return cached
        observations = fetch_hourly_weather(self.plant.location, date_str, date_str)
        with self._lock:
            self._weather[date_str] = observations
        return observations

    def soiling_for(self, date_str: str, use_weather: bool) -> tuple[float, str, tuple[str, ...]]:
        """Fleet soiling on a date: from real rainfall if we have it, else a stated default."""
        if not use_weather:
            return (soiling_after_dry_days(DEFAULT_DRY_DAYS, self.scenario.soiling_parameters),
                    f"demo_default_{DEFAULT_DRY_DAYS:.0f}_dry_days",
                    ("soiling_is_a_stated_demo_default_not_derived_from_rainfall_load_weather_for_the_real_value",))
        with self._lock:
            cached = self._soiling.get(date_str)
        if cached is not None:
            return cached

        end = date.fromisoformat(date_str)
        start = end - timedelta(days=SOILING_LOOKBACK_DAYS)
        observations = fetch_hourly_weather(self.plant.location, start.isoformat(), end.isoformat())
        series = simulate_soiling(
            [o.timestamp_utc for o in observations],
            [o.precipitation_mm for o in observations],
            self.scenario.soiling_parameters,
        )
        warnings = list(series.warnings)
        if series.cleaning_events == 0:
            # The dry spell is longer than the window we looked at, so the
            # figure below is a floor, not an estimate of the true value.
            warnings.append(
                f"no_rain_within_the_{SOILING_LOOKBACK_DAYS}_day_lookback_so_soiling_is_a_lower_bound")
        result = (series.final_soiling_loss_fraction,
                  f"rainfall_history_{SOILING_LOOKBACK_DAYS}_days_{series.cleaning_events}_cleaning_events",
                  tuple(dict.fromkeys(warnings)))
        with self._lock:
            self._soiling[date_str] = result
        return result

    # -- scenario --------------------------------------------------------

    def set_faults(self, requested: dict, now: datetime) -> dict:
        """Replace the declared fault set. `{}` clears everything."""
        known = {entry["id"] for entry in self.plant.topology()["blocks"]}
        faults = {}
        for block_id, spec in requested.items():
            if block_id not in known:
                raise ValueError(f"Unknown block id: {block_id}")
            if not isinstance(spec, dict):
                raise ValueError(f"Fault for {block_id} must be an object")
            kind = spec.get("kind")
            since = spec.get("since_utc")
            faults[block_id] = BlockFault(
                kind=kind,
                since_utc=datetime.fromisoformat(since) if since else now,
                severity=float(spec.get("severity", 1.0)),
                note=str(spec.get("note", "")),
            )
        with self._lock:
            self.scenario = replace(self.scenario, faults=faults)
        return self.scenario.payload()


def _local_to_utc(plant: Plant, date_str: str, hour_decimal: float) -> datetime:
    calendar_date = date.fromisoformat(date_str)
    local_midnight = datetime(calendar_date.year, calendar_date.month, calendar_date.day,
                              tzinfo=ZoneInfo(plant.location.timezone))
    return (local_midnight + timedelta(seconds=round(hour_decimal * 3600))).astimezone(timezone.utc)


@dataclass(frozen=True)
class Scene:
    """One instant, fully resolved: the twin's expectation and the plant's reality."""

    timestamp_utc: datetime
    weather_source: str
    cloud_cover_fraction: float
    conditions: Conditions
    position: object
    poa: object
    plane: object
    model: object
    actual: object
    residuals: object
    soiling_fraction: float
    soiling_source: str
    soiling_warnings: tuple[str, ...]


def build_scene(
    session: Session, timestamp_utc: datetime, dni: float, dhi: float, ghi: float,
    air_temperature_c: float, wind_speed_m_s: float, cloud_cover_fraction: float,
    weather_source: str, soiling_fraction: float, soiling_source: str,
    soiling_warnings: tuple[str, ...] = (),
) -> Scene:
    """Run the full Step 4 -> 12 chain once, for both the model and the plant."""
    plant = session.plant
    scenario = session.scenario
    position = solar_position(plant.location, timestamp_utc)
    poa = transpose_to_poa(dni, dhi, ghi, position.zenith_deg, position.azimuth_deg,
                           plant.layout.tilt_deg, plant.layout.azimuth_deg,
                           plant.location.ground_albedo, timestamp_utc)
    plane = apply_array_losses(poa, position.zenith_deg, position.azimuth_deg, plant.row_geometry)
    conditions = Conditions(plane.effective_poa_global_w_m2, air_temperature_c, wind_speed_m_s)

    healthy = nominal_state(plant, timestamp_utc, scenario.commissioned_utc,
                            soiling_loss_fraction=soiling_fraction, source="nominal")
    model = simulate_fleet(plant, conditions, plane, healthy, timestamp_utc)

    if scenario.faults:
        faulted = with_faults(healthy, scenario.faults, source="declared_faults")
        actual = simulate_fleet(plant, conditions, plane, faulted, timestamp_utc)
    else:
        actual = model

    residuals = compare_to_model(synthesize_measurement(actual), model)
    return Scene(timestamp_utc, weather_source, cloud_cover_fraction, conditions,
                 position, poa, plane, model, actual, residuals, soiling_fraction, soiling_source,
                 soiling_warnings)


def _block_payload(scene: Scene) -> list[dict]:
    """Per-block rows: what it is doing, what it should be, and the gap."""
    rows = []
    for actual, expected, residual in zip(scene.actual.blocks, scene.model.blocks, scene.residuals.blocks):
        rows.append({
            "block_id": actual.block_id,
            "inverter_id": actual.inverter_id,
            "fault_kind": actual.fault_kind,
            "fault_note": actual.fault_note,
            "status": actual.status,
            "array_dc_mw": actual.array_dc_mw,
            "available_dc_mw": actual.available_dc_mw,
            "inverter_ac_mw": actual.inverter_ac_mw,
            "expected_ac_mw": expected.inverter_ac_mw,
            "measured_ac_mw": residual.measured_ac_mw,
            "relative_to_peers": residual.relative_to_peers,
            "estimated_shortfall_mw": residual.estimated_shortfall_mw,
            "flag": residual.flag,
            "soiling_loss_fraction": actual.soiling_loss_fraction,
            "degradation_loss_fraction": actual.degradation_loss_fraction,
            "string_availability": actual.string_availability,
            "clipping_loss_mw": actual.clipping_loss_mw,
            "age_years": actual.age_years,
        })
    return rows


def scene_payload(session: Session, scene: Scene) -> dict:
    plant = session.plant
    electrical = evaluate_dc_electrical(plant, scene.conditions)
    actual, model = scene.actual, scene.model
    return {
        "timestamp_utc": scene.timestamp_utc.isoformat(),
        "weather_source": scene.weather_source,
        "cloud_cover_fraction": scene.cloud_cover_fraction,
        "conditions": asdict(scene.conditions),
        "solar_position": asdict(scene.position),
        "poa": asdict(scene.poa),
        "array_plane": {
            "shaded_row_fraction": scene.plane.shaded_row_fraction,
            "profile_angle_deg": scene.plane.profile_angle_deg,
            "incidence_angle_modifier": scene.plane.incidence_angle_modifier,
            "effective_poa_global_w_m2": scene.plane.effective_poa_global_w_m2,
            "shading_loss_w_m2": scene.plane.shading_loss_w_m2,
            "reflection_loss_w_m2": scene.plane.reflection_loss_w_m2,
            "ground_coverage_ratio": scene.plane.ground_coverage_ratio,
            "assumptions": list(scene.plane.assumptions),
        },
        "electrical": asdict(electrical),
        "grid": {
            "net_export_mw": actual.net_export_mw,
            "curtailment_mw": actual.curtailment_mw,
            "expected_net_export_mw": model.net_export_mw,
            "plant_inverter_ac_mw": actual.plant_inverter_ac_mw,
            "available_dc_mw": actual.available_dc_mw,
            "array_dc_mw": actual.array_dc_mw,
        },
        "fleet": {
            "blocks": _block_payload(scene),
            "waterfall": [{"step": name, "mw": value} for name, value in actual.loss_waterfall],
            "faulted_block_ids": list(actual.faulted_block_ids),
            "cell_temperature_c": actual.cell_temperature_c,
            "warnings": list(actual.warnings),
            "assumptions": list(actual.assumptions),
        },
        "residuals": {
            "flagged_block_ids": list(scene.residuals.flagged_block_ids),
            "fleet_median_ratio": scene.residuals.fleet_median_ratio,
            "measured_plant_ac_mw": scene.residuals.measured_plant_ac_mw,
            "modelled_plant_ac_mw": scene.residuals.modelled_plant_ac_mw,
            "estimated_total_shortfall_mw": scene.residuals.estimated_total_shortfall_mw,
            "tolerance": scene.residuals.tolerance,
            "warnings": list(scene.residuals.warnings),
            "assumptions": list(scene.residuals.assumptions),
        },
        "state": {
            "soiling_loss_fraction": scene.soiling_fraction,
            "soiling_source": scene.soiling_source,
            "warnings": list(scene.soiling_warnings),
            "commissioned_utc": session.scenario.commissioned_utc.isoformat(),
            "age_years": actual.blocks[0].age_years,
            "degradation_loss_fraction": actual.blocks[0].degradation_loss_fraction,
        },
    }


def synthetic_scene(session: Session, timestamp_utc: datetime, local_hour: float) -> Scene:
    position = solar_position(session.plant.location, timestamp_utc)
    split = clear_sky_irradiance(session.plant.location, timestamp_utc, position.zenith_deg)
    soiling, source, notes = session.soiling_for(timestamp_utc.date().isoformat(), use_weather=False)
    return build_scene(
        session, timestamp_utc, split.dni_w_m2, split.dhi_w_m2, split.ghi_w_m2,
        synthesize_air_temperature_c(local_hour), 2.0, SYNTHETIC_CLOUD_COVER_FRACTION,
        "synthetic_clear_sky", soiling, source, notes,
    )


def snapshot_response(session: Session, date_str: str, hour_decimal: float) -> dict:
    """Synthetic clear-sky path: instant, no network, illustrative only."""
    if not 0 <= hour_decimal < 24:
        raise ValueError("hour must be within [0, 24)")
    timestamp_utc = _local_to_utc(session.plant, date_str, hour_decimal)
    return scene_payload(session, synthetic_scene(session, timestamp_utc, hour_decimal))


def weather_day_response(session: Session, date_str: str, steps_per_hour: int = 4) -> list[dict]:
    """Real path: actual Open-Meteo weather for the given UTC date.

    The archive is hourly, but the frontend's slider moves in 15 minute steps,
    so playback held each observation for four frames and then jumped. With
    `steps_per_hour` above 1 the gaps between observations are filled in
    (`interpolation.py`), and every sample says whether it was measured or
    interpolated. Measured samples are never rewritten.
    """
    if steps_per_hour not in (1, 2, 4):
        raise ValueError("steps must be 1, 2 or 4")
    observations = session.weather_day(date_str)
    measured_at = {observation.timestamp_utc for observation in observations}
    if steps_per_hour > 1:
        observations = interpolate_weather(session.plant.location, observations, steps_per_hour)
    notes = list(interpolation_warnings(steps_per_hour))

    soiling, soiling_source, soiling_notes = session.soiling_for(date_str, use_weather=True)
    payloads = []
    for observation in observations:
        measured = observation.timestamp_utc in measured_at
        scene = build_scene(
            session, observation.timestamp_utc, observation.dni_w_m2, observation.dhi_w_m2,
            observation.ghi_w_m2, observation.air_temperature_c, observation.wind_speed_10m_m_s,
            observation.cloud_cover_percent / 100,
            "open_meteo" if measured else "open_meteo_interpolated",
            soiling, soiling_source, soiling_notes,
        )
        payload = scene_payload(session, scene)
        payload["precipitation_mm"] = observation.precipitation_mm
        payload["measured"] = measured
        payload["weather_warnings"] = [] if measured else notes
        payloads.append(payload)
    return payloads


def day_response(session: Session, date_str: str, steps_per_hour: int = 2, record: bool = False) -> dict:
    """One local day at a fixed step: the power curve, plus energy, yield and PR."""
    if steps_per_hour not in (1, 2, 4):
        raise ValueError("steps_per_hour must be 1, 2 or 4")
    plant = session.plant
    # 00:00 local through 24:00 local inclusive, so the trapezoid covers the
    # whole day; the final sample is the next day's midnight.
    midnight = _local_to_utc(plant, date_str, 0)
    hours = [index / steps_per_hour for index in range(24 * steps_per_hour + 1)]
    timestamps = [midnight + timedelta(hours=hour) for hour in hours]

    scenes = [synthetic_scene(session, when, hour % 24) for when, hour in zip(timestamps, hours)]
    series = [{
        "local_hour": hour,
        "timestamp_utc": scene.timestamp_utc.isoformat(),
        "net_export_mw": scene.actual.net_export_mw,
        "expected_net_export_mw": scene.model.net_export_mw,
        "poa_w_m2": scene.poa.poa_global_w_m2,
        "effective_poa_w_m2": scene.plane.effective_poa_global_w_m2,
        "cell_temperature_c": scene.actual.cell_temperature_c,
        "shaded_row_fraction": scene.plane.shaded_row_fraction,
        "flagged": len(scene.residuals.flagged_block_ids),
    } for scene, hour in zip(scenes, hours)]

    totals = summarise_energy(
        plant, timestamps,
        [s.poa.poa_global_w_m2 for s in scenes],
        [s.plane.effective_poa_global_w_m2 for s in scenes],
        [s.actual.available_dc_mw for s in scenes],
        [s.actual.plant_inverter_ac_mw for s in scenes],
        [s.actual.net_export_mw for s in scenes],
        [s.actual.curtailment_mw for s in scenes],
    )
    expected = summarise_energy(
        plant, timestamps,
        [s.poa.poa_global_w_m2 for s in scenes],
        [s.plane.effective_poa_global_w_m2 for s in scenes],
        [s.model.available_dc_mw for s in scenes],
        [s.model.plant_inverter_ac_mw for s in scenes],
        [s.model.net_export_mw for s in scenes],
        [s.model.curtailment_mw for s in scenes],
    )

    if record and session.history is not None:
        session.history.record_day(date_str, "synthetic_clear_sky", totals, scenes[0].soiling_fraction)
        hours_per_step = 1 / steps_per_hour
        for index, block in enumerate(scenes[0].actual.blocks):
            energy = sum(s.actual.blocks[index].inverter_ac_mw for s in scenes) * hours_per_step
            flagged = sum(hours_per_step for s in scenes
                          if block.block_id in s.residuals.flagged_block_ids)
            relatives = [s.residuals.blocks[index].relative_to_peers for s in scenes
                         if s.residuals.blocks[index].relative_to_peers is not None]
            session.history.record_block_day(date_str, block.block_id, energy, flagged,
                                             min(relatives) if relatives else None)

    return {
        "date": date_str,
        "steps_per_hour": steps_per_hour,
        "series": series,
        "energy": asdict(totals),
        "expected_energy": asdict(expected),
        "energy_shortfall_mwh": expected.export_energy_mwh - totals.export_energy_mwh,
        "recorded": bool(record and session.history is not None),
    }


def history_response(session: Session, days: int = 90) -> dict:
    if session.history is None:
        return {"enabled": False, "daily": [], "worst_blocks": [], "open_faults": []}
    return {
        "enabled": True,
        "daily": session.history.daily_energy(days),
        "worst_blocks": [asdict(trend) for trend in session.history.worst_blocks(days)],
        "open_faults": session.history.open_faults(),
    }


def plant_payload(session: Session) -> dict:
    plant = session.plant
    geometry = plant.row_geometry
    return {
        "summary": plant.summary(),
        "layout": asdict(plant.layout),
        "location": asdict(plant.location),
        "topology": plant.topology(),
        "geometry": {
            "ground_coverage_ratio": geometry.ground_coverage_ratio,
            "row_pitch_m": geometry.row_pitch_m,
            "collector_slant_m": geometry.collector_slant_m,
            "shading_onset_profile_angle_deg": geometry.shading_onset_profile_angle_deg,
            "module_length_m": plant.module.length_m,
            "module_width_m": plant.module.width_m,
        },
        "scenario": session.scenario.payload(),
    }


def make_handler(plant: Plant, history_path: str | Path | None = None):
    session = Session(plant, history_path)

    class Handler(SimpleHTTPRequestHandler):
        session_ref = session

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

        def _send_json(self, payload, status: int = 200) -> None:
            body = json.dumps(payload, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            try:
                if parsed.path == "/api/plant":
                    return self._send_json(plant_payload(session))
                if parsed.path == "/api/snapshot":
                    return self._send_json(snapshot_response(session, query["date"], float(query["hour"])))
                if parsed.path == "/api/weather-day":
                    return self._send_json(weather_day_response(
                        session, query["date"], int(query.get("steps", 4))))
                if parsed.path == "/api/day":
                    return self._send_json(day_response(
                        session, query["date"], int(query.get("steps", 2)),
                        record=query.get("record") == "1"))
                if parsed.path == "/api/history":
                    return self._send_json(history_response(session, int(query.get("days", 90))))
                if parsed.path == "/api/scenario":
                    return self._send_json(session.scenario.payload())
            except (KeyError, ValueError, TypeError, OSError) as exc:
                return self._send_json({"error": str(exc)}, status=400)
            return super().do_GET()

        def do_POST(self):
            parsed = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 1_000_000:
                    raise ValueError("A JSON body is required")
                body = json.loads(self.rfile.read(length))
                if parsed.path == "/api/scenario/faults":
                    if not isinstance(body, dict):
                        raise ValueError("Body must be an object of block id to fault")
                    return self._send_json(session.set_faults(body, datetime.now(timezone.utc)))
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
                return self._send_json({"error": str(exc)}, status=400)
            return self._send_json({"error": "Unknown endpoint"}, status=404)

    return Handler


def run(config_path: str = "config/plant.json", port: int = 8000,
        history_path: str | None = "history.sqlite3") -> None:
    plant = load_plant(config_path)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(plant, history_path))
    print(f"Serving the {plant.name} digital twin at http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the solar plant digital twin frontend")
    parser.add_argument("--config", default="config/plant.json")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--history", default="history.sqlite3",
                        help="SQLite file for daily energy and fault history ('' to disable)")
    args = parser.parse_args()
    run(args.config, args.port, args.history or None)


if __name__ == "__main__":
    main()
