"""Static plant model. Units are explicit; ratings are not generation forecasts."""

from dataclasses import asdict, dataclass, fields
import json
import math
from pathlib import Path

from .array_geometry import RowGeometry
from .location import Location


@dataclass(frozen=True)
class Module:
    reference: str
    power_w: float
    vmp_v: float
    voc_v: float
    imp_a: float
    isc_a: float
    voc_temperature_coefficient_per_c: float
    power_temperature_coefficient_per_c: float
    isc_temperature_coefficient_per_c: float
    max_system_voltage_v: float
    length_m: float
    width_m: float
    first_year_degradation_fraction: float
    annual_degradation_fraction: float


@dataclass(frozen=True)
class Inverter:
    reference: str
    active_power_limit_mw: float
    apparent_power_at_50c_mva: float
    max_dc_voltage_v: float
    nominal_mppt_min_v: float
    nominal_mppt_max_v: float
    max_dc_current_a: float
    max_short_circuit_current_a: float
    ac_voltage_kv: float


@dataclass(frozen=True)
class Layout:
    blocks: int
    strings_per_block: int
    modules_per_string: int
    block_transformer_mva: float
    collector_voltage_kv: float
    grid_voltage_kv: float
    grid_transformer_mva: float
    export_limit_mw: float
    tilt_deg: float
    azimuth_deg: float
    modules_along_slope: int
    row_pitch_m: float


@dataclass(frozen=True)
class DesignEnvelope:
    minimum_cell_temperature_c: float
    voltage_margin_fraction: float
    operating_current_multiplier: float
    short_circuit_current_multiplier: float


@dataclass(frozen=True)
class Plant:
    name: str
    module: Module
    inverter: Inverter
    layout: Layout
    design_envelope: DesignEnvelope
    location: Location

    def __post_init__(self):
        self.validate()

    @property
    def module_count(self) -> int:
        return self.layout.blocks * self.layout.strings_per_block * self.layout.modules_per_string

    @property
    def dc_capacity_mwp(self) -> float:
        return self.module_count * self.module.power_w / 1_000_000

    @property
    def ac_capacity_mw(self) -> float:
        return self.layout.blocks * self.inverter.active_power_limit_mw

    def degradation_loss_fraction(self, age_years: float) -> float:
        """Module warranty degradation at a given age. A guaranteed floor, not a measurement."""
        from .assets import degradation_loss_fraction
        return degradation_loss_fraction(
            age_years, self.module.first_year_degradation_fraction, self.module.annual_degradation_fraction,
        )

    @property
    def module_efficiency(self) -> float:
        """STC power per unit module area, at the 1000 W/m2 reference irradiance."""
        return self.module.power_w / (self.module.length_m * self.module.width_m * 1000)

    @property
    def row_geometry(self) -> RowGeometry:
        """Cross-section of one repeating row pair, for Step 8 shading."""
        return RowGeometry(
            collector_slant_m=self.layout.modules_along_slope * self.module.length_m,
            row_pitch_m=self.layout.row_pitch_m,
            tilt_deg=self.layout.tilt_deg,
            surface_azimuth_deg=self.layout.azimuth_deg,
        )

    @property
    def cold_string_voc_v(self) -> float:
        m, e = self.module, self.design_envelope
        return self.layout.modules_per_string * m.voc_v * (
            1 + m.voc_temperature_coefficient_per_c * (e.minimum_cell_temperature_c - 25)
        ) * (1 + e.voltage_margin_fraction)

    def validate(self) -> None:
        m, i, l, e = self.module, self.inverter, self.layout, self.design_envelope
        for obj in (self, m, i):
            label = obj.name if isinstance(obj, Plant) else obj.reference
            if not isinstance(label, str) or not label.strip():
                raise ValueError("Plant name and equipment references must be nonempty strings")
        signed = {"voc_temperature_coefficient_per_c", "power_temperature_coefficient_per_c",
                  "minimum_cell_temperature_c"}
        zero_allowed = {"tilt_deg", "azimuth_deg", "voltage_margin_fraction"}
        for obj in (m, i, l, e):
            for f in fields(obj):
                if f.name == "reference":
                    continue
                value = getattr(obj, f.name)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"{f.name} must be a finite number")
                if f.name not in signed and (value < 0 or (value == 0 and f.name not in zero_allowed)):
                    raise ValueError(f"{f.name} must be positive (or zero where allowed)")
        for name in ("blocks", "strings_per_block", "modules_per_string", "modules_along_slope"):
            if type(getattr(l, name)) is not int:
                raise ValueError(f"{name} must be an integer")
        if not (-40 <= e.minimum_cell_temperature_c <= 25):
            raise ValueError("Minimum cell temperature must be between -40 and 25 C")
        if not (0 <= l.tilt_deg <= 90 and 0 <= l.azimuth_deg < 360):
            raise ValueError("Invalid surface orientation")
        if not 0 < m.first_year_degradation_fraction < 0.2 or not 0 < m.annual_degradation_fraction < 0.2:
            raise ValueError("Degradation fractions must be small positive fractions per year")
        if not 0.05 <= self.module_efficiency <= 0.30:
            raise ValueError("Module power and physical size imply an impossible efficiency")
        self.row_geometry  # rejects a row pitch shorter than the row's own footprint
        if not (-0.01 < m.voc_temperature_coefficient_per_c < 0 and
                -0.01 < m.power_temperature_coefficient_per_c < 0):
            raise ValueError("Temperature coefficients must be negative fractions per C")
        if m.vmp_v >= m.voc_v or m.imp_a >= m.isc_a:
            raise ValueError("Module operating voltage/current must be below open/short circuit values")
        if abs(m.vmp_v * m.imp_a - m.power_w) > 0.01 * m.power_w:
            raise ValueError("Module power must agree with Vmp * Imp within rounding tolerance")
        if min(e.operating_current_multiplier, e.short_circuit_current_multiplier) < 1:
            raise ValueError("Current design multipliers must be at least one")
        if not (i.nominal_mppt_min_v < i.nominal_mppt_max_v < i.max_dc_voltage_v):
            raise ValueError("Invalid inverter voltage limits")
        if self.cold_string_voc_v > min(m.max_system_voltage_v, i.max_dc_voltage_v):
            raise ValueError("Cold string open-circuit voltage exceeds equipment limit")
        stc_vmp = l.modules_per_string * m.vmp_v
        if not i.nominal_mppt_min_v <= stc_vmp <= i.nominal_mppt_max_v:
            raise ValueError("STC string voltage is outside nominal-power MPPT range")
        if l.strings_per_block * m.imp_a * e.operating_current_multiplier > i.max_dc_current_a:
            raise ValueError("Design operating current exceeds inverter limit")
        if l.strings_per_block * m.isc_a * e.short_circuit_current_multiplier > i.max_short_circuit_current_a:
            raise ValueError("Design short-circuit current exceeds inverter limit")
        if i.active_power_limit_mw > i.apparent_power_at_50c_mva:
            raise ValueError("Active power exceeds inverter apparent power rating")
        if l.block_transformer_mva < i.apparent_power_at_50c_mva:
            raise ValueError("Block transformer is smaller than inverter rating")
        if l.export_limit_mw > min(self.ac_capacity_mw, l.grid_transformer_mva):
            raise ValueError("Export limit exceeds installed AC or grid transformer capacity")
        if not i.ac_voltage_kv < l.collector_voltage_kv < l.grid_voltage_kv:
            raise ValueError("Transformer voltage levels must increase toward the grid")

    def summary(self) -> dict:
        return {
            "name": self.name,
            "blocks": self.layout.blocks,
            "strings": self.layout.blocks * self.layout.strings_per_block,
            "modules": self.module_count,
            "dc_capacity_mwp": self.dc_capacity_mwp,
            "ac_capacity_mw": self.ac_capacity_mw,
            "export_limit_mw": self.layout.export_limit_mw,
            "dc_ac_ratio": self.dc_capacity_mwp / self.ac_capacity_mw,
            "ground_coverage_ratio": self.row_geometry.ground_coverage_ratio,
            "row_pitch_m": self.layout.row_pitch_m,
            "collector_slant_m": self.row_geometry.collector_slant_m,
            "module_efficiency": self.module_efficiency,
            "warranty_retention_at_25_years": 1 - self.degradation_loss_fraction(25),
            "tilt_deg": self.layout.tilt_deg,
            "cold_string_voc_with_margin_v": round(self.cold_string_voc_v, 3),
            "location": self.location.name,
            "scope": "Static plant ratings; these values are not simulated output",
        }

    def topology(self) -> dict:
        """Stable asset IDs for later SCADA tags; repeated modules remain aggregated."""
        return {
            "grid_connection_id": "POI-001",
            "grid_transformer_id": "GT-001",
            "collector_bus_id": "BUS-33KV-001",
            "blocks": [
                {
                    "id": f"BLK-{n:03}",
                    "inverter_id": f"INV-{n:03}",
                    "transformer_id": f"TR-{n:03}",
                    "collector_bus_id": "BUS-33KV-001",
                    "string_ids": [f"BLK-{n:03}-STR-{s:03}"
                                   for s in range(1, self.layout.strings_per_block + 1)],
                    "modules_per_string": self.layout.modules_per_string,
                }
                for n in range(1, self.layout.blocks + 1)
            ],
        }


def load_plant(path: str | Path) -> Plant:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for key, cls in (("module", Module), ("inverter", Inverter), ("layout", Layout),
                     ("design_envelope", DesignEnvelope), ("location", Location)):
        data[key] = cls(**data[key])
    return Plant(**data)


def configuration(plant: Plant) -> dict:
    return asdict(plant)
