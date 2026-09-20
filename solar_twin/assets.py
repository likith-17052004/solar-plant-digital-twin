"""Step 9: per-block asset state - the first thing in this project that is not
a pure function of the weather.

Up to Step 8 every block was interchangeable: `dc.simulate_dc` computed one
number and repeated it 20 times. That is fine for a design calculation and
useless for a twin, because a twin's whole job is to tell you that block 7 is
not like the others. This module gives each block an identity that persists:
how dirty it is, how old it is, and what is currently wrong with it.

Design rule here: **a fault is the single source of truth.** Availability,
derating and excess soiling are all derived properties of the fault, never
independently settable fields, so a block's state cannot contradict itself.

Honest limits:

* State is below the block only in aggregate. `strings_disconnected` removes
  a *fraction* of a block's strings; it does not track which ones, and the
  remaining strings are assumed unaffected. Real string outages also shift
  the inverter's MPPT operating point, which is not modeled here.
* Degradation is the module warranty curve (a bounding guarantee), not a
  measured degradation rate, and is applied uniformly within a block.
* Faults are declared, not detected. Nothing in this module infers a fault
  from data; Step 11 compares model against measurement and leaves the
  diagnosis to the reader.
* No repair model, no failure statistics, no mean-time-between-failures. A
  fault starts when you say it starts and lasts until you remove it.
"""

from dataclasses import dataclass, replace
from datetime import datetime

from .plant import Plant
from .solar_position import _require_utc
from .validation import number_in_range

INVERTER_OFFLINE = "inverter_offline"
INVERTER_DERATED = "inverter_derated"
STRINGS_DISCONNECTED = "strings_disconnected"
LOCALISED_SOILING = "localised_soiling"

FAULT_KINDS = (INVERTER_OFFLINE, INVERTER_DERATED, STRINGS_DISCONNECTED, LOCALISED_SOILING)

FAULT_DESCRIPTIONS = {
    INVERTER_OFFLINE: "Inverter is not exporting; the whole block is dark.",
    INVERTER_DERATED: "Inverter is running below nameplate, e.g. degraded cooling.",
    STRINGS_DISCONNECTED: "Some strings are open-circuit; the block's DC is reduced pro rata.",
    LOCALISED_SOILING: "This block is dirtier than the fleet, e.g. next to a site road.",
}

MAXIMUM_SOILING_FRACTION = 0.9


@dataclass(frozen=True)
class BlockFault:
    """One declared problem on one block. Severity is the fraction affected."""

    kind: str
    since_utc: datetime
    severity: float = 1.0
    note: str = ""

    def __post_init__(self):
        if self.kind not in FAULT_KINDS:
            raise ValueError(f"Unknown fault kind {self.kind!r}; expected one of {FAULT_KINDS}")
        _require_utc(self.since_utc)
        number_in_range("severity", self.severity, 0, 1)
        if self.kind == INVERTER_OFFLINE and self.severity != 1.0:
            raise ValueError("inverter_offline is all or nothing; severity must be 1.0")
        if not isinstance(self.note, str):
            raise ValueError("note must be a string")

    @property
    def description(self) -> str:
        return FAULT_DESCRIPTIONS[self.kind]


@dataclass(frozen=True)
class BlockState:
    """What is true about one inverter block right now."""

    block_id: str
    inverter_id: str
    commissioned_utc: datetime
    soiling_loss_fraction: float = 0.0
    fault: BlockFault | None = None

    def __post_init__(self):
        for name in ("block_id", "inverter_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be a nonempty string")
        _require_utc(self.commissioned_utc)
        number_in_range("soiling_loss_fraction", self.soiling_loss_fraction, 0, MAXIMUM_SOILING_FRACTION)
        if self.fault is not None and not isinstance(self.fault, BlockFault):
            raise ValueError("fault must be a BlockFault or None")

    @property
    def healthy(self) -> bool:
        return self.fault is None

    @property
    def inverter_available(self) -> bool:
        return not (self.fault is not None and self.fault.kind == INVERTER_OFFLINE)

    @property
    def string_availability(self) -> float:
        """Fraction of the block's strings still delivering DC."""
        if self.fault is not None and self.fault.kind == STRINGS_DISCONNECTED:
            return 1 - self.fault.severity
        return 1.0

    @property
    def inverter_capacity_fraction(self) -> float:
        """Fraction of inverter nameplate available for export."""
        if not self.inverter_available:
            return 0.0
        if self.fault is not None and self.fault.kind == INVERTER_DERATED:
            return 1 - self.fault.severity
        return 1.0

    @property
    def effective_soiling_loss_fraction(self) -> float:
        """Fleet soiling, plus any extra this particular block is carrying."""
        if self.fault is not None and self.fault.kind == LOCALISED_SOILING:
            return min(MAXIMUM_SOILING_FRACTION, self.soiling_loss_fraction + self.fault.severity)
        return self.soiling_loss_fraction

    def age_years(self, as_of_utc: datetime) -> float:
        _require_utc(as_of_utc)
        return max(0.0, (as_of_utc - self.commissioned_utc).total_seconds() / (365.2425 * 86400))


def degradation_loss_fraction(age_years: float, first_year: float, annual: float) -> float:
    """Module warranty degradation: a larger first-year step, then linear.

    This is the manufacturer's *guaranteed floor*, not a measured rate. Real
    modules usually do better, and real degradation is not perfectly linear.
    """
    number_in_range("age_years", age_years, 0, 100)
    number_in_range("first_year", first_year, 0, 0.2)
    number_in_range("annual", annual, 0, 0.2)
    if age_years <= 1:
        return first_year * age_years
    return min(0.9, first_year + annual * (age_years - 1))


@dataclass(frozen=True)
class PlantState:
    """Every block's state at one instant, plus where these values came from."""

    as_of_utc: datetime
    blocks: tuple[BlockState, ...]
    source: str
    scope: str = "Declared asset state; faults are asserted by the caller, not detected from data"
    assumptions: tuple[str, ...] = (
        "string_outages_tracked_as_a_fraction_of_a_block_not_as_identified_strings",
        "degradation_is_the_module_warranty_floor_not_a_measured_rate",
        "faults_are_declared_and_persist_until_removed_no_failure_or_repair_statistics",
        "mppt_operating_point_shift_from_partial_string_loss_not_modeled",
    )

    def __post_init__(self):
        _require_utc(self.as_of_utc)
        if not self.blocks:
            raise ValueError("A plant state must contain at least one block")
        ids = [block.block_id for block in self.blocks]
        if len(set(ids)) != len(ids):
            raise ValueError("Block ids must be unique")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a nonempty string")

    @property
    def faulted(self) -> tuple[BlockState, ...]:
        return tuple(block for block in self.blocks if not block.healthy)

    def block(self, block_id: str) -> BlockState:
        for block in self.blocks:
            if block.block_id == block_id:
                return block
        raise KeyError(f"No such block: {block_id!r}")


def nominal_state(
    plant: Plant,
    as_of_utc: datetime,
    commissioned_utc: datetime,
    soiling_loss_fraction: float = 0.0,
    source: str = "nominal",
) -> PlantState:
    """Every block healthy, same age, same dust. The twin's baseline belief."""
    _require_utc(as_of_utc)
    _require_utc(commissioned_utc)
    if commissioned_utc > as_of_utc:
        raise ValueError("commissioned_utc must not be after as_of_utc")
    blocks = tuple(
        BlockState(
            block_id=entry["id"],
            inverter_id=entry["inverter_id"],
            commissioned_utc=commissioned_utc,
            soiling_loss_fraction=soiling_loss_fraction,
        )
        for entry in plant.topology()["blocks"]
    )
    return PlantState(as_of_utc=as_of_utc, blocks=blocks, source=source)


def with_faults(state: PlantState, faults: dict[str, BlockFault], source: str | None = None) -> PlantState:
    """Return a copy of `state` with the given block ids faulted."""
    known = {block.block_id for block in state.blocks}
    unknown = set(faults) - known
    if unknown:
        raise KeyError(f"Unknown block ids: {sorted(unknown)}")
    blocks = tuple(
        replace(block, fault=faults.get(block.block_id, block.fault)) for block in state.blocks
    )
    return replace(state, blocks=blocks, source=source or state.source)


def with_soiling(state: PlantState, soiling_loss_fraction: float) -> PlantState:
    """Apply one fleet-wide soiling level, leaving faults untouched."""
    number_in_range("soiling_loss_fraction", soiling_loss_fraction, 0, MAXIMUM_SOILING_FRACTION)
    blocks = tuple(replace(block, soiling_loss_fraction=soiling_loss_fraction) for block in state.blocks)
    return replace(state, blocks=blocks)
