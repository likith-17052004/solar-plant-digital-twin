"""Step 5: approximate string voltage/current, layered on the Step 2 power model.

The PVWatts power model (dc.py) cannot see hot-string undervoltage, cold
overvoltage, or inverter current-limit conditions - those require an actual
operating voltage/current, not just power. This module adds that, without
changing the DC power number: it is a diagnostic layer that flags electrical
infeasibility rather than guessing what an inverter/controller would actually
do about it (shut down, limit power, trip protection, ...), which remains
future work.
"""

from dataclasses import dataclass

from .dc import Conditions, DCResult, ThermalParameters, simulate_dc
from .plant import Plant


@dataclass(frozen=True)
class DCElectricalResult:
    dc: DCResult
    string_vmp_v: float
    string_voc_v: float
    module_operating_current_a: float
    block_operating_current_a: float
    warnings: tuple[str, ...]
    scope: str = "String voltage/current feasibility; does not change DC power output"
    assumptions: tuple[str, ...] = (
        "vmp_temperature_coefficient_assumed_equal_to_voc_temperature_coefficient",
        "imp_temperature_coefficient_assumed_equal_to_isc_temperature_coefficient",
        "operating_current_assumed_linear_in_effective_irradiance",
        "voltage_irradiance_dependence_ignored_except_at_zero_light",
        "voltage_current_product_not_constrained_to_pvwatts_power",
        "full_iv_curve_startup_voltage_and_mismatch_not_modeled",
    )


def evaluate_dc_electrical(
    plant: Plant, conditions: Conditions, thermal_parameters: ThermalParameters = ThermalParameters(),
) -> DCElectricalResult:
    dc = simulate_dc(plant, conditions, thermal_parameters)
    m, i, l = plant.module, plant.inverter, plant.layout
    delta_t = dc.cell_temperature_c - 25

    string_vmp = l.modules_per_string * m.vmp_v * (1 + m.voc_temperature_coefficient_per_c * delta_t)
    string_voc = l.modules_per_string * m.voc_v * (1 + m.voc_temperature_coefficient_per_c * delta_t)
    module_current = (m.imp_a * (1 + m.isc_temperature_coefficient_per_c * delta_t)
                      * dc.effective_irradiance_w_m2 / 1000)
    block_current = module_current * l.strings_per_block
    if dc.effective_irradiance_w_m2 == 0:
        string_vmp = string_voc = 0.0

    warnings = list(dc.warnings)
    producing = dc.effective_irradiance_w_m2 > 0
    if producing and string_vmp < i.nominal_mppt_min_v:
        warnings.append("string_voltage_below_mppt_range")
    if producing and string_vmp > i.nominal_mppt_max_v:
        warnings.append("string_voltage_above_mppt_range")
    if string_voc > min(m.max_system_voltage_v, i.max_dc_voltage_v):
        warnings.append("open_circuit_voltage_exceeds_system_limit")
    if producing and block_current > i.max_dc_current_a:
        warnings.append("operating_current_exceeds_inverter_limit")

    return DCElectricalResult(
        dc=dc,
        string_vmp_v=string_vmp,
        string_voc_v=string_voc,
        module_operating_current_a=module_current,
        block_operating_current_a=block_current,
        warnings=tuple(warnings),
    )
