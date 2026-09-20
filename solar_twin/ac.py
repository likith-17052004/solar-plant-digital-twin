"""Conditional inverter-terminal AC model at unity power factor.

Uses a generic PVWatts efficiency curve and an explicitly assumed temperature
curve. DC voltage/current feasibility and grid-side losses remain separate work.
"""

from dataclasses import dataclass
import math

from .dc import Conditions, DCResult, ThermalParameters, _number_in_range, simulate_dc
from .plant import Inverter, Plant


@dataclass(frozen=True)
class InverterParameters:
    nominal_efficiency: float = 0.987
    maximum_efficiency: float = 0.99
    derating_start_air_temperature_c: float = 50.0
    derating_fraction_per_c: float = 0.02
    minimum_operating_air_temperature_c: float = -35.0
    maximum_operating_air_temperature_c: float = 60.0

    def __post_init__(self):
        _number_in_range("nominal_efficiency", self.nominal_efficiency, 0.5, 1)
        _number_in_range("maximum_efficiency", self.maximum_efficiency, self.nominal_efficiency, 1)
        _number_in_range("derating_fraction_per_c", self.derating_fraction_per_c, 0, 1)
        for name in ("minimum_operating_air_temperature_c", "derating_start_air_temperature_c",
                     "maximum_operating_air_temperature_c"):
            _number_in_range(name, getattr(self, name), -60, 70)
        if not (self.minimum_operating_air_temperature_c < self.derating_start_air_temperature_c
                < self.maximum_operating_air_temperature_c):
            raise ValueError("Inverter temperature limits must satisfy minimum < derating start < maximum")


@dataclass(frozen=True)
class InverterResult:
    available_dc_mw: float
    accepted_dc_mw: float
    unharvested_dc_mw: float
    conversion_loss_mw: float
    ac_output_mw: float
    operating_efficiency: float | None
    nominal_ac_limit_mw: float
    temperature_ac_limit_mw: float
    temperature_capacity_factor: float
    nameplate_clipping_ac_mw: float
    temperature_reduction_ac_mw: float
    status: str


@dataclass(frozen=True)
class BlockAC:
    block_id: str
    inverter_id: str
    inverter: InverterResult


@dataclass(frozen=True)
class ACResult:
    dc: DCResult
    inverter_parameters: InverterParameters
    inverter_air_temperature_c: float
    inverter_air_temperature_source: str
    blocks: tuple[BlockAC, ...]
    plant_inverter_ac_mw: float
    plant_accepted_dc_mw: float
    plant_unharvested_dc_mw: float
    plant_conversion_loss_mw: float
    plant_nameplate_clipping_ac_mw: float
    plant_temperature_reduction_ac_mw: float
    warnings: tuple[str, ...]
    scope: str = "Conditional AC at inverter terminals, unity power factor; not net grid export"
    assumptions: tuple[str, ...] = (
        "generic_efficiency_curve_with_assumed_nominal_efficiency",
        "assumed_linear_temperature_derating_and_out_of_range_shutdown",
        "dc_voltage_current_and_mppt_feasibility_not_modeled",
        "transformers_cables_auxiliaries_and_export_control_not_modeled",
    )


def _unconstrained_ac_mw(dc_mw: float, nominal_ac_mw: float, p: InverterParameters) -> float:
    """PVWatts conversion before clipping, rearranged to avoid division by DC.

    Inverter Pdc0 is AC rating / nominal efficiency, NOT the array DC nameplate.
    This helper is evaluated only on the bounded lower branch (zeta <= 10).
    """
    # Written out rather than calling pvlib.inverter.pvwatts, deliberately.
    # pvlib's version clips its output at nameplate AC, and this helper is the
    # *unclipped* branch on purpose: convert_inverter solves backwards from a
    # clipped AC target to the DC actually consumed, which needs the curve to
    # keep rising past nameplate. Swapping it in silently broke that solve and
    # three tests caught it. Below clipping the two agree to 4e-16 MW, which
    # tests/test_pvlib_agreement.py pins.
    dc_reference = nominal_ac_mw / p.nominal_efficiency
    zeta = dc_mw / dc_reference
    converted = p.nominal_efficiency / 0.9637 * dc_reference * (
        -0.0162 * zeta * zeta + 0.9858 * zeta - 0.0059
    )
    return max(0.0, min(converted, dc_mw * p.maximum_efficiency))


def convert_inverter(
    available_dc_mw: float,
    inverter_air_temperature_c: float,
    inverter: Inverter,
    parameters: InverterParameters = InverterParameters(),
) -> InverterResult:
    """Convert one block's available DC, with a reconciled power budget.

    During clipping we solve for the DC needed for delivered AC. Unharvested
    potential is not counted as inverter heat. This is a power-only dispatch
    approximation, not an IV operating-point or voltage-protection calculation.
    """
    nominal = min(inverter.active_power_limit_mw, inverter.apparent_power_at_50c_mva)
    _number_in_range("nominal_ac_limit_mw", nominal, 1e-9, 1000)
    _number_in_range("available_dc_mw", available_dc_mw, 0, 10 * nominal / parameters.nominal_efficiency)
    _number_in_range("inverter_air_temperature_c", inverter_air_temperature_c, -60, 70)
    p = parameters
    outside_range = not p.minimum_operating_air_temperature_c <= inverter_air_temperature_c <= p.maximum_operating_air_temperature_c
    temperature_factor = 0.0 if outside_range else max(
        0.0, 1 - p.derating_fraction_per_c * max(0.0, inverter_air_temperature_c - p.derating_start_air_temperature_c)
    )
    thermal_limit = nominal * temperature_factor
    potential_ac = _unconstrained_ac_mw(available_dc_mw, nominal, p)
    after_nameplate = min(potential_ac, nominal)
    ac_output = min(after_nameplate, thermal_limit)

    if ac_output == 0:
        accepted = 0.0
    elif ac_output == potential_ac:
        accepted = available_dc_mw
    else:
        # The target never exceeds nominal AC, so the nominal DC reference is
        # an upper bracket on the increasing part of the conversion curve.
        low, high = 0.0, min(available_dc_mw, nominal / p.nominal_efficiency)
        for _ in range(60):
            middle = (low + high) / 2
            if _unconstrained_ac_mw(middle, nominal, p) < ac_output:
                low = middle
            else:
                high = middle
        accepted = high

    if available_dc_mw == 0:
        status = "no_dc_power"
    elif temperature_factor == 0:
        status = "temperature_shutdown"
    elif ac_output == 0:
        status = "low_power_standby"
    else:
        status = "operating"
    return InverterResult(
        available_dc_mw=available_dc_mw,
        accepted_dc_mw=accepted,
        unharvested_dc_mw=available_dc_mw - accepted,
        conversion_loss_mw=accepted - ac_output,
        ac_output_mw=ac_output,
        operating_efficiency=ac_output / accepted if accepted > 0 else None,
        nominal_ac_limit_mw=nominal,
        temperature_ac_limit_mw=thermal_limit,
        temperature_capacity_factor=temperature_factor,
        nameplate_clipping_ac_mw=potential_ac - after_nameplate,
        temperature_reduction_ac_mw=after_nameplate - ac_output,
        status=status,
    )


def simulate_ac(
    plant: Plant,
    conditions: Conditions,
    parameters: InverterParameters = InverterParameters(),
    thermal_parameters: ThermalParameters = ThermalParameters(),
    inverter_air_temperature_c: float | None = None,
) -> ACResult:
    """Weather → available DC → inverter-terminal AC, uniform conditions per block."""
    air = conditions.air_temperature_c if inverter_air_temperature_c is None else inverter_air_temperature_c
    dc = simulate_dc(plant, conditions, thermal_parameters)
    blocks = tuple(
        BlockAC(block.block_id, f"INV-{n:03}", convert_inverter(block.available_dc_mw, air, plant.inverter, parameters))
        for n, block in enumerate(dc.blocks, start=1)
    )
    warnings = list(dc.warnings)
    if any(b.inverter.temperature_capacity_factor < 1 for b in blocks):
        warnings.append("assumed_inverter_temperature_limit_active")

    def total(field: str) -> float:
        return math.fsum(getattr(b.inverter, field) for b in blocks)

    return ACResult(
        dc=dc, inverter_parameters=parameters,
        inverter_air_temperature_c=air,
        inverter_air_temperature_source="ambient_weather" if inverter_air_temperature_c is None else "provided",
        blocks=blocks,
        plant_inverter_ac_mw=total("ac_output_mw"),
        plant_accepted_dc_mw=total("accepted_dc_mw"),
        plant_unharvested_dc_mw=total("unharvested_dc_mw"),
        plant_conversion_loss_mw=total("conversion_loss_mw"),
        plant_nameplate_clipping_ac_mw=total("nameplate_clipping_ac_mw"),
        plant_temperature_reduction_ac_mw=total("temperature_reduction_ac_mw"),
        warnings=tuple(warnings),
    )
