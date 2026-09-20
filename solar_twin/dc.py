"""Steady-state, front-side DC potential: Sandia temperature + PVWatts v5 DC.

This is available array power, before inverter voltage/current limits or AC
conversion. Inputs must already contain irradiance on the tilted panel plane.
"""

from dataclasses import dataclass
import math

from .plant import Plant
from .validation import number_in_range as _number_in_range


@dataclass(frozen=True)
class Conditions:
    poa_irradiance_w_m2: float
    air_temperature_c: float
    wind_speed_10m_m_s: float
    soiling_loss_fraction: float = 0.0
    cell_temperature_override_c: float | None = None

    def __post_init__(self):
        _number_in_range("poa_irradiance_w_m2", self.poa_irradiance_w_m2, 0, 2000)
        _number_in_range("air_temperature_c", self.air_temperature_c, -60, 70)
        _number_in_range("wind_speed_10m_m_s", self.wind_speed_10m_m_s, 0, 75)
        _number_in_range("soiling_loss_fraction", self.soiling_loss_fraction, 0, 1)
        if self.cell_temperature_override_c is not None:
            _number_in_range("cell_temperature_override_c", self.cell_temperature_override_c, -60, 120)


@dataclass(frozen=True)
class ThermalParameters:
    """Sandia representative open-rack glass/glass coefficients, not a Trina fit."""

    a: float = -3.47
    b: float = -0.0594
    cell_module_delta_at_1000w_c: float = 3.0

    def __post_init__(self):
        _number_in_range("a", self.a, -10, -1)
        _number_in_range("b", self.b, -1, 0)
        _number_in_range("cell_module_delta_at_1000w_c", self.cell_module_delta_at_1000w_c, 0, 20)


@dataclass(frozen=True)
class BlockDC:
    block_id: str
    available_dc_mw: float


@dataclass(frozen=True)
class DCResult:
    conditions: Conditions
    thermal_parameters: ThermalParameters
    cell_temperature_c: float
    cell_temperature_source: str
    effective_irradiance_w_m2: float
    temperature_power_factor: float
    module_available_dc_w: float
    plant_available_dc_mw: float
    blocks: tuple[BlockDC, ...]
    warnings: tuple[str, ...]
    scope: str = "Available front-side DC power; inverter limits and AC export are not simulated"


def estimate_cell_temperature(
    conditions: Conditions, parameters: ThermalParameters = ThermalParameters(),
) -> float:
    """Sandia module temperature plus irradiance-dependent cell/back offset.

    Uses incident POA (before soiling), 10 m wind, and ambient temperature.
    The caller may supply cell temperature instead for measured data or STC.
    """
    if conditions.cell_temperature_override_c is not None:
        return conditions.cell_temperature_override_c
    irradiance = conditions.poa_irradiance_w_m2
    module_temperature = conditions.air_temperature_c + irradiance * math.exp(
        parameters.a + parameters.b * conditions.wind_speed_10m_m_s
    )
    return module_temperature + irradiance / 1000 * parameters.cell_module_delta_at_1000w_c


def simulate_dc(
    plant: Plant, conditions: Conditions, parameters: ThermalParameters = ThermalParameters(),
) -> DCResult:
    """Evaluate one uniform snapshot across every block, with no hidden losses.

    PVWatts v5 DC equation uses effective irradiance and cell temperature.
    Its low-light response is linear; the older quadratic correction is omitted.
    Soiling is a uniform optical reduction, not electrical shading/mismatch.
    """
    temperature = estimate_cell_temperature(conditions, parameters)
    effective = conditions.poa_irradiance_w_m2 * (1 - conditions.soiling_loss_fraction)
    factor = max(0.0, 1 + plant.module.power_temperature_coefficient_per_c * (temperature - 25))
    module_power = plant.module.power_w * effective / 1000 * factor
    block_power = module_power * plant.layout.modules_per_string * plant.layout.strings_per_block / 1e6
    blocks = tuple(BlockDC(f"BLK-{n:03}", block_power) for n in range(1, plant.layout.blocks + 1))
    warnings = []
    if temperature < plant.design_envelope.minimum_cell_temperature_c:
        warnings.append("cell_temperature_below_static_design_minimum")
    if not -40 <= temperature <= 85:
        warnings.append("cell_temperature_outside_module_datasheet_range")
    return DCResult(
        conditions=conditions,
        thermal_parameters=parameters,
        cell_temperature_c=temperature,
        cell_temperature_source="provided" if conditions.cell_temperature_override_c is not None else "sandia_estimate",
        effective_irradiance_w_m2=effective,
        temperature_power_factor=factor,
        module_available_dc_w=module_power,
        plant_available_dc_mw=math.fsum(block.available_dc_mw for block in blocks),
        blocks=blocks,
        warnings=tuple(warnings),
    )
