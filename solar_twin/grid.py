"""Step 6: grid-side losses and net export at the point of interconnection (POI).

Layers block-transformer, collector-cable, grid-transformer, and HV-cable
losses plus auxiliary/parasitic load onto the existing, unmodified
`simulate_ac` (same wrapping pattern ac.py already uses over dc.py), finally
producing a net export the plant's `export_limit_mw` can actually curtail.

Unlike the module/inverter (real cited datasheets), this project has no real
transformer or cable product behind its equipment - the README already says
transformer sizing is a project assumption, not a specification of an
existing installation. So the loss fractions here are generic,
industry-typical two-parameter transformer loss ratios and flat cable-loss
percentages, not a manufacturer's guaranteed figures.
"""

from dataclasses import dataclass

from .ac import ACResult, InverterParameters, simulate_ac
from .dc import Conditions, ThermalParameters
from .plant import Plant
from .validation import number_in_range


@dataclass(frozen=True)
class GridLossParameters:
    block_transformer_no_load_loss_fraction: float = 0.002
    block_transformer_load_loss_fraction: float = 0.006
    collector_cable_loss_fraction: float = 0.01
    grid_transformer_no_load_loss_fraction: float = 0.001
    grid_transformer_load_loss_fraction: float = 0.003
    hv_cable_loss_fraction: float = 0.002
    auxiliary_load_per_block_kw: float = 5.0
    plant_auxiliary_load_kw: float = 50.0

    def __post_init__(self):
        for name in ("block_transformer_no_load_loss_fraction", "block_transformer_load_loss_fraction",
                     "collector_cable_loss_fraction", "grid_transformer_no_load_loss_fraction",
                     "grid_transformer_load_loss_fraction", "hv_cable_loss_fraction"):
            number_in_range(name, getattr(self, name), 0, 0.05)
        for name in ("auxiliary_load_per_block_kw", "plant_auxiliary_load_kw"):
            number_in_range(name, getattr(self, name), 0, 1000)
        if (self.block_transformer_no_load_loss_fraction + self.block_transformer_load_loss_fraction) >= 1:
            raise ValueError("Block transformer no-load + load loss fractions must be below 1")
        if (self.grid_transformer_no_load_loss_fraction + self.grid_transformer_load_loss_fraction) >= 1:
            raise ValueError("Grid transformer no-load + load loss fractions must be below 1")


@dataclass(frozen=True)
class GridExportResult:
    ac: ACResult
    block_transformer_output_mw: float
    block_transformer_loss_mw: float
    collector_cable_loss_mw: float
    grid_transformer_output_mw: float
    grid_transformer_loss_mw: float
    hv_cable_loss_mw: float
    auxiliary_load_mw: float
    net_export_before_curtailment_mw: float
    net_export_mw: float
    curtailment_mw: float
    warnings: tuple[str, ...]
    scope: str = "Net export at the point of interconnection; not measured plant telemetry"
    assumptions: tuple[str, ...] = (
        "transformer_and_cable_loss_fractions_are_generic_industry_typical_values_not_a_specific_datasheet",
        "cable_losses_modeled_as_flat_fractions_not_derived_from_conductor_length_or_gauge",
        "no_load_transformer_losses_and_auxiliary_load_applied_only_while_producing_ac_standby_grid_draw_deferred",
        "unity_power_factor_assumed_throughout_reactive_power_and_var_support_not_modeled",
    )


def _transformer_stage(input_mw: float, rated_mva: float, no_load_frac: float, load_frac: float):
    """One two-parameter (no-load + load-dependent) transformer loss stage."""
    loading = input_mw / rated_mva
    loss = no_load_frac * rated_mva + load_frac * rated_mva * loading ** 2
    output = input_mw - loss
    if output < 0:
        return 0.0, input_mw, True
    return output, loss, False


@dataclass(frozen=True)
class GridCascade:
    """The loss chain from inverter terminals to the point of interconnection."""

    block_transformer_output_mw: float
    block_transformer_loss_mw: float
    collector_cable_loss_mw: float
    grid_transformer_output_mw: float
    grid_transformer_loss_mw: float
    hv_cable_loss_mw: float
    idle_transformer_loss_mw: float
    auxiliary_load_mw: float
    net_export_before_curtailment_mw: float
    net_export_mw: float
    curtailment_mw: float
    warnings: tuple[str, ...]


ZERO_CASCADE = GridCascade(*([0.0] * 11), warnings=())


def apply_grid_losses(
    plant: Plant,
    block_inverter_ac_mw: list[float],
    parameters: GridLossParameters = GridLossParameters(),
) -> GridCascade:
    """Run the loss chain over each block's own inverter output.

    Taking a per-block list rather than one plant total is what lets Step 10
    simulate blocks that are not identical: a block whose inverter is offline
    contributes no power but its transformer stays energised on the collector
    network, so it still costs the plant its no-load loss.
    """
    l, p = plant.layout, parameters
    if len(block_inverter_ac_mw) != l.blocks:
        raise ValueError(f"Expected {l.blocks} block AC values, got {len(block_inverter_ac_mw)}")
    for value in block_inverter_ac_mw:
        number_in_range("block_inverter_ac_mw", value, 0, l.block_transformer_mva)
    if sum(block_inverter_ac_mw) <= 0:
        return ZERO_CASCADE

    warnings = []
    block_transformer_output_mw = block_transformer_loss_mw = idle_transformer_loss_mw = 0.0
    for block_input in block_inverter_ac_mw:
        if block_input <= 0:
            # Energised but not producing: the no-load loss is still drawn.
            idle_transformer_loss_mw += p.block_transformer_no_load_loss_fraction * l.block_transformer_mva
            continue
        output, loss, floored = _transformer_stage(
            block_input, l.block_transformer_mva,
            p.block_transformer_no_load_loss_fraction, p.block_transformer_load_loss_fraction,
        )
        if floored:
            warnings.append("block_transformer_no_load_loss_exceeds_input")
        block_transformer_output_mw += output
        block_transformer_loss_mw += loss
    if idle_transformer_loss_mw > 0:
        warnings.append("idle_block_transformers_still_drawing_no_load_losses")

    collector_cable_loss_mw = block_transformer_output_mw * p.collector_cable_loss_fraction
    collector_bus_mw = block_transformer_output_mw - collector_cable_loss_mw

    grid_transformer_output_mw, grid_transformer_loss_mw, grid_floored = _transformer_stage(
        collector_bus_mw, l.grid_transformer_mva,
        p.grid_transformer_no_load_loss_fraction, p.grid_transformer_load_loss_fraction,
    )
    if grid_floored:
        warnings.append("grid_transformer_no_load_loss_exceeds_input")

    hv_cable_loss_mw = grid_transformer_output_mw * p.hv_cable_loss_fraction
    poi_mw = grid_transformer_output_mw - hv_cable_loss_mw

    auxiliary_load_mw = (p.auxiliary_load_per_block_kw * l.blocks + p.plant_auxiliary_load_kw) / 1000
    auxiliary_load_mw += idle_transformer_loss_mw
    if auxiliary_load_mw > poi_mw:
        warnings.append("auxiliary_demand_exceeds_generation_grid_import_not_modeled")
        auxiliary_load_mw = poi_mw
    net_export_before_curtailment_mw = poi_mw - auxiliary_load_mw

    net_export_mw = min(net_export_before_curtailment_mw, l.export_limit_mw)
    curtailment_mw = net_export_before_curtailment_mw - net_export_mw
    if curtailment_mw > 0:
        warnings.append("export_curtailed_at_point_of_interconnection")

    return GridCascade(
        block_transformer_output_mw=block_transformer_output_mw,
        block_transformer_loss_mw=block_transformer_loss_mw,
        collector_cable_loss_mw=collector_cable_loss_mw,
        grid_transformer_output_mw=grid_transformer_output_mw,
        grid_transformer_loss_mw=grid_transformer_loss_mw,
        hv_cable_loss_mw=hv_cable_loss_mw,
        idle_transformer_loss_mw=idle_transformer_loss_mw,
        auxiliary_load_mw=auxiliary_load_mw,
        net_export_before_curtailment_mw=net_export_before_curtailment_mw,
        net_export_mw=net_export_mw,
        curtailment_mw=curtailment_mw,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def simulate_grid_export(
    plant: Plant,
    conditions: Conditions,
    inverter_parameters: InverterParameters = InverterParameters(),
    thermal_parameters: ThermalParameters = ThermalParameters(),
    grid_loss_parameters: GridLossParameters = GridLossParameters(),
    inverter_air_temperature_c: float | None = None,
) -> GridExportResult:
    """Uniform-fleet path: every block identical. See fleet.py for per-block state."""
    ac = simulate_ac(plant, conditions, inverter_parameters, thermal_parameters, inverter_air_temperature_c)
    per_block = ac.plant_inverter_ac_mw / plant.layout.blocks
    cascade = apply_grid_losses(plant, [per_block] * plant.layout.blocks, grid_loss_parameters)
    return GridExportResult(
        ac=ac,
        block_transformer_output_mw=cascade.block_transformer_output_mw,
        block_transformer_loss_mw=cascade.block_transformer_loss_mw,
        collector_cable_loss_mw=cascade.collector_cable_loss_mw,
        grid_transformer_output_mw=cascade.grid_transformer_output_mw,
        grid_transformer_loss_mw=cascade.grid_transformer_loss_mw,
        hv_cable_loss_mw=cascade.hv_cable_loss_mw,
        auxiliary_load_mw=cascade.auxiliary_load_mw,
        net_export_before_curtailment_mw=cascade.net_export_before_curtailment_mw,
        net_export_mw=cascade.net_export_mw,
        curtailment_mw=cascade.curtailment_mw,
        warnings=tuple(dict.fromkeys([*ac.warnings, *cascade.warnings])),
    )
