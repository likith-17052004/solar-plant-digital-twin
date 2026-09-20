"""Step 10: simulate every block separately, under its own state.

This is the step that turns the model into a twin. `dc.simulate_dc` computes
one block and repeats it; here each block carries its own soiling, age and
fault, so blocks stop being interchangeable and the plant total becomes a sum
over genuinely different machines.

It also produces the artifact plant engineers actually read: a **loss
waterfall** from nameplate irradiance down to net export, itemised and
closing exactly (there is a test for that). Every megawatt that does not
reach the grid is attributed to a named cause rather than vanishing into an
unexplained derate factor.

Layering, all reusing earlier steps unmodified:

    POA (Step 4) -> shading + reflection (Step 8) -> per-block DC (Step 2,
    called once per distinct soiling level) -> per-block inverter (Step 3)
    -> grid cascade (Step 6)

Honest limits beyond those the earlier steps already declare:

* A block is the finest resolution. Strings inside a block are aggregated,
  so string-level mismatch and MPPT shifts are still not modeled.
* Every block sees the same irradiance and the same air temperature. Real
  sites have cloud shadows crossing them and a hot corner by the boundary
  wall; spatial weather variation across the site is not modeled.
* Cell temperature is computed from incident POA before shading, so a shaded
  block is not credited with running cooler.
"""

from dataclasses import dataclass, replace
from datetime import datetime
import math

from .ac import InverterParameters, InverterResult, convert_inverter
from .array_geometry import ArrayPlaneResult
from .assets import PlantState
from .dc import Conditions, ThermalParameters, simulate_dc
from .grid import GridCascade, GridLossParameters, apply_grid_losses
from .plant import Plant

_ZERO_INVERTER = InverterResult(
    available_dc_mw=0.0, accepted_dc_mw=0.0, unharvested_dc_mw=0.0, conversion_loss_mw=0.0,
    ac_output_mw=0.0, operating_efficiency=None, nominal_ac_limit_mw=0.0,
    temperature_ac_limit_mw=0.0, temperature_capacity_factor=0.0,
    nameplate_clipping_ac_mw=0.0, temperature_reduction_ac_mw=0.0, status="offline",
)


@dataclass(frozen=True)
class BlockOutcome:
    """One block's simulated result, and why it differs from its neighbours."""

    block_id: str
    inverter_id: str
    fault_kind: str | None
    fault_note: str
    age_years: float
    soiling_loss_fraction: float
    degradation_loss_fraction: float
    string_availability: float
    inverter_capacity_fraction: float
    cell_temperature_c: float
    nameplate_dc_mw: float
    array_dc_mw: float
    available_dc_mw: float
    inverter_ac_mw: float
    shading_loss_mw: float
    reflection_loss_mw: float
    sky_masking_loss_mw: float
    temperature_loss_mw: float
    soiling_loss_mw: float
    degradation_loss_mw: float
    availability_loss_mw: float
    outage_loss_mw: float
    clipping_loss_mw: float
    conversion_loss_mw: float
    status: str

    @property
    def healthy(self) -> bool:
        return self.fault_kind is None


@dataclass(frozen=True)
class FleetResult:
    """The whole plant, block by block, with an itemised loss waterfall."""

    timestamp_utc: str
    state_source: str
    conditions: Conditions
    array_plane: ArrayPlaneResult
    blocks: tuple[BlockOutcome, ...]
    cell_temperature_c: float
    nameplate_dc_mw: float
    array_dc_mw: float
    available_dc_mw: float
    plant_inverter_ac_mw: float
    cascade: GridCascade
    net_export_mw: float
    curtailment_mw: float
    loss_waterfall: tuple[tuple[str, float], ...]
    faulted_block_ids: tuple[str, ...]
    warnings: tuple[str, ...]
    scope: str = "Per-block simulated output under declared asset state; not measured plant telemetry"
    assumptions: tuple[str, ...] = (
        "block_is_the_finest_resolution_string_level_mismatch_and_mppt_shift_not_modeled",
        "all_blocks_see_identical_irradiance_and_air_temperature_no_spatial_weather_variation",
        "cell_temperature_uses_incident_poa_so_shaded_blocks_are_not_credited_with_running_cooler",
    )

    @property
    def block_ids(self) -> tuple[str, ...]:
        return tuple(block.block_id for block in self.blocks)

    def block(self, block_id: str) -> BlockOutcome:
        for block in self.blocks:
            if block.block_id == block_id:
                return block
        raise KeyError(f"No such block: {block_id!r}")


def simulate_fleet(
    plant: Plant,
    conditions: Conditions,
    array_plane: ArrayPlaneResult,
    state: PlantState,
    timestamp_utc: datetime,
    inverter_parameters: InverterParameters = InverterParameters(),
    thermal_parameters: ThermalParameters = ThermalParameters(),
    grid_loss_parameters: GridLossParameters = GridLossParameters(),
    inverter_air_temperature_c: float | None = None,
) -> FleetResult:
    """Run each block under its own state and sum the plant up from the parts.

    `conditions` must already carry the post-shading effective POA from
    `array_plane`; pass `conditions.soiling_loss_fraction = 0` and let each
    block apply its own soiling, or the loss will be counted twice.
    """
    if len(state.blocks) != plant.layout.blocks:
        raise ValueError(f"State has {len(state.blocks)} blocks, plant has {plant.layout.blocks}")
    if conditions.soiling_loss_fraction:
        raise ValueError("Pass clean conditions to simulate_fleet; soiling is per block")

    air = conditions.air_temperature_c if inverter_air_temperature_c is None else inverter_air_temperature_c
    modules_per_block = plant.layout.modules_per_string * plant.layout.strings_per_block
    block_capacity_mw = modules_per_block * plant.module.power_w / 1e6
    incident_poa = array_plane.incident.poa_global_w_m2
    effective_poa = array_plane.effective_poa_global_w_m2

    # Shading and reflection are identical across blocks, so they are shared
    # rather than recomputed; only soiling varies, and usually only on a
    # faulted block, so one simulate_dc call per distinct soiling level is
    # enough to cover the fleet.
    clean = simulate_dc(plant, replace(conditions, soiling_loss_fraction=0.0), thermal_parameters)
    cell_temperature_c = clean.cell_temperature_c
    temperature_factor = clean.temperature_power_factor
    clean_dc_per_block = block_capacity_mw * effective_poa / 1000 * temperature_factor
    nameplate_dc_per_block = block_capacity_mw * incident_poa / 1000

    shading_share = block_capacity_mw * array_plane.shading_loss_w_m2 / 1000
    reflection_share = block_capacity_mw * array_plane.reflection_loss_w_m2 / 1000
    masking_share = block_capacity_mw * array_plane.sky_masking_loss_w_m2 / 1000
    temperature_share = block_capacity_mw * effective_poa / 1000 * (1 - temperature_factor)

    outcomes, block_ac, warnings = [], [], list(clean.warnings)
    for block_state in state.blocks:
        soiling = block_state.effective_soiling_loss_fraction
        age = block_state.age_years(state.as_of_utc)
        degradation = plant.degradation_loss_fraction(age)
        availability = block_state.string_availability
        capacity_fraction = block_state.inverter_capacity_fraction

        after_soiling = clean_dc_per_block * (1 - soiling)
        after_degradation = after_soiling * (1 - degradation)
        # What this block's array makes, whether or not anything can take it.
        array_dc_mw = after_degradation * availability
        # An offline inverter does not make its array's DC disappear - it
        # strands it. Keep the two apart so the waterfall can name the cause.
        inverter_online = capacity_fraction > 0
        available_dc_mw = array_dc_mw if inverter_online else 0.0

        if not inverter_online or available_dc_mw <= 0:
            inverter = _ZERO_INVERTER
        else:
            derated = replace(
                plant.inverter,
                active_power_limit_mw=plant.inverter.active_power_limit_mw * capacity_fraction,
                apparent_power_at_50c_mva=plant.inverter.apparent_power_at_50c_mva * capacity_fraction,
            )
            inverter = convert_inverter(available_dc_mw, air, derated, inverter_parameters)

        outcomes.append(BlockOutcome(
            block_id=block_state.block_id,
            inverter_id=block_state.inverter_id,
            fault_kind=block_state.fault.kind if block_state.fault else None,
            fault_note=block_state.fault.note if block_state.fault else "",
            age_years=age,
            soiling_loss_fraction=soiling,
            degradation_loss_fraction=degradation,
            string_availability=availability,
            inverter_capacity_fraction=capacity_fraction,
            cell_temperature_c=cell_temperature_c,
            nameplate_dc_mw=nameplate_dc_per_block,
            array_dc_mw=array_dc_mw,
            available_dc_mw=available_dc_mw,
            inverter_ac_mw=inverter.ac_output_mw,
            shading_loss_mw=shading_share,
            reflection_loss_mw=reflection_share,
            sky_masking_loss_mw=masking_share,
            temperature_loss_mw=temperature_share,
            soiling_loss_mw=clean_dc_per_block - after_soiling,
            degradation_loss_mw=after_soiling - after_degradation,
            availability_loss_mw=after_degradation - array_dc_mw,
            outage_loss_mw=array_dc_mw - available_dc_mw,
            clipping_loss_mw=inverter.unharvested_dc_mw,
            conversion_loss_mw=inverter.conversion_loss_mw,
            status=inverter.status,
        ))
        block_ac.append(inverter.ac_output_mw)

    cascade = apply_grid_losses(plant, block_ac, grid_loss_parameters)
    warnings.extend(array_plane.warnings)
    warnings.extend(cascade.warnings)
    if any(not outcome.healthy for outcome in outcomes):
        warnings.append("one_or_more_blocks_are_running_under_a_declared_fault")

    def total(field: str) -> float:
        return math.fsum(getattr(outcome, field) for outcome in outcomes)

    plant_inverter_ac_mw = math.fsum(block_ac)
    waterfall = (
        ("nameplate_dc_at_incident_poa", total("nameplate_dc_mw")),
        ("row_shading", -total("shading_loss_mw")),
        ("glass_reflection", -total("reflection_loss_mw")),
        ("sky_masked_by_rows", -total("sky_masking_loss_mw")),
        ("cell_temperature", -total("temperature_loss_mw")),
        ("soiling", -total("soiling_loss_mw")),
        ("module_degradation", -total("degradation_loss_mw")),
        ("string_availability", -total("availability_loss_mw")),
        ("inverter_outage", -total("outage_loss_mw")),
        ("inverter_clipping", -total("clipping_loss_mw")),
        ("inverter_conversion", -total("conversion_loss_mw")),
        ("block_transformers", -cascade.block_transformer_loss_mw),
        ("collector_cables", -cascade.collector_cable_loss_mw),
        ("grid_transformer", -cascade.grid_transformer_loss_mw),
        ("hv_cables", -cascade.hv_cable_loss_mw),
        ("auxiliary_load", -cascade.auxiliary_load_mw),
        ("curtailment", -cascade.curtailment_mw),
    )

    return FleetResult(
        timestamp_utc=timestamp_utc.isoformat(),
        state_source=state.source,
        conditions=conditions,
        array_plane=array_plane,
        blocks=tuple(outcomes),
        cell_temperature_c=cell_temperature_c,
        nameplate_dc_mw=total("nameplate_dc_mw"),
        array_dc_mw=total("array_dc_mw"),
        available_dc_mw=total("available_dc_mw"),
        plant_inverter_ac_mw=plant_inverter_ac_mw,
        cascade=cascade,
        net_export_mw=cascade.net_export_mw,
        curtailment_mw=cascade.curtailment_mw,
        loss_waterfall=waterfall,
        faulted_block_ids=tuple(o.block_id for o in outcomes if not o.healthy),
        warnings=tuple(dict.fromkeys(warnings)),
    )
