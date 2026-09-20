"""Weather observations -> POA irradiance -> the existing DC/AC snapshot models.

Each timestamp remains an independent steady-state snapshot, exactly like the
hand-written scenarios in Steps 2-3: no elapsed time or energy integration is
introduced here. Turning a series of instantaneous power snapshots into
energy (MWh) is a distinct, still-future step.
"""

from dataclasses import asdict, dataclass

from .ac import ACResult, InverterParameters, simulate_ac
from .dc import Conditions, DCResult, ThermalParameters, simulate_dc
from .irradiance import POAResult, transpose_to_poa
from .plant import Plant
from .solar_position import SolarPosition, solar_position
from .weather import WeatherObservation


@dataclass(frozen=True)
class WeatherSnapshot:
    timestamp_utc: str
    solar_position: SolarPosition
    poa: POAResult
    result: DCResult | ACResult


def simulate_weather_series(
    plant: Plant,
    observations: list[WeatherObservation],
    ac: bool = False,
    inverter_parameters: InverterParameters = InverterParameters(),
    thermal_parameters: ThermalParameters = ThermalParameters(),
) -> list[WeatherSnapshot]:
    snapshots = []
    for obs in observations:
        position = solar_position(plant.location, obs.timestamp_utc)
        poa = transpose_to_poa(
            dni_w_m2=obs.dni_w_m2,
            dhi_w_m2=obs.dhi_w_m2,
            ghi_w_m2=obs.ghi_w_m2,
            solar_zenith_deg=position.zenith_deg,
            solar_azimuth_deg=position.azimuth_deg,
            surface_tilt_deg=plant.layout.tilt_deg,
            surface_azimuth_deg=plant.layout.azimuth_deg,
            ground_albedo=plant.location.ground_albedo,
        )
        conditions = Conditions(
            poa_irradiance_w_m2=poa.poa_global_w_m2,
            air_temperature_c=obs.air_temperature_c,
            wind_speed_10m_m_s=obs.wind_speed_10m_m_s,
        )
        result = (simulate_ac(plant, conditions, inverter_parameters, thermal_parameters) if ac
                  else simulate_dc(plant, conditions, thermal_parameters))
        snapshots.append(WeatherSnapshot(
            timestamp_utc=obs.timestamp_utc.isoformat(),
            solar_position=position,
            poa=poa,
            result=result,
        ))
    return snapshots


def snapshot_to_dict(snapshot: WeatherSnapshot) -> dict:
    return {
        "timestamp_utc": snapshot.timestamp_utc,
        "solar_position": asdict(snapshot.solar_position),
        "poa": asdict(snapshot.poa),
        "result": asdict(snapshot.result),
    }
