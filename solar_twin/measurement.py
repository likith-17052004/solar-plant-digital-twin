"""Step 12: compare the model against measurement, and let the gap talk.

This is the step that makes the project a *twin* rather than a simulator. A
simulator answers "what should this plant produce?". A twin answers "what is
this plant producing that it should not be?" - and the only way to ask that
is to put a modeled block next to a measured one and look at the difference.

There is no physical plant here, so measurement is synthesised: run the fleet
under a *true* state that contains faults, add sensor noise, and hand the
result to a twin that believes the *nominal* healthy state. Everything the
twin then finds, it finds the same way it would on a real site. The synthetic
origin is stamped on every result as `source`, and never presented as real.

**Detection is by peer comparison, not by absolute threshold.** Each block's
measured/modeled ratio is compared against the *fleet median* of that ratio.
This matters: if the irradiance input is 8% low, every block reads 8% low and
an absolute threshold would flag all twenty. Dividing by the median cancels
any error common to the whole plant and leaves only what is block-specific -
the same trick real PV monitoring uses, and the reason this works at all on
top of a reanalysis weather feed that is itself approximate.

Honest limits:

* This flags anomalies; it does not diagnose them. A low block could be
  soiled, partly disconnected, mis-wired, or have a broken sensor. The report
  names the symptom and stops.
* Peer comparison is blind to a fault that affects every block equally.
  Fleet-wide soiling, for example, is invisible here by construction - it
  moves the median with everything else.
* **Clipping hides faults.** This plant runs a 1.3 DC/AC ratio, so around
  midday every healthy block is pinned at inverter nameplate. A block losing
  less than the clipped headroom still reports full output and cannot be seen
  at all until the sun drops. The report warns when the model is clipping;
  sensitivity is genuinely worse at those hours and no amount of tuning here
  fixes it.
* It needs daylight and a working fleet. With fewer than three producing
  blocks the median is not a meaningful reference and the report says so.
* Noise is independent Gaussian per block per timestamp. Real measurement
  error is correlated in time and biased by calibration drift.
* No persistence logic. A block flagged for one hour reads the same as one
  flagged for a month; ranking by duration is left to the history layer.
"""

from dataclasses import dataclass
from datetime import datetime
import random
import statistics
import zlib

from .fleet import FleetResult
from .validation import number_in_range

NORMAL = "normal"
UNDERPERFORMING = "underperforming"
NOT_PRODUCING = "not_producing"
ABOVE_MODEL = "above_model"

DEFAULT_TOLERANCE = 0.05
MINIMUM_PEERS = 3

# A dark inverter does not report exactly zero - sensor noise puts it at a few
# kilowatts - so "not producing" has to be a fraction of what was expected,
# not an absolute floor, or an offline block is misfiled as merely behind.
NOT_PRODUCING_FRACTION = 0.02

# Below this, a block's modelled output is small enough that the sensor noise
# on it dominates any real difference: at dusk a 5 MW block making 5 kW is
# within a couple of noise sigma of its neighbours, and peer ratios swing
# wildly for no physical reason. Comparing there produced six "underperforming"
# blocks on a clear evening with the plant at 0.1 MW. Ratios are still
# reported; they are just not turned into flags.
MINIMUM_FLAGGABLE_MW = 0.05


@dataclass(frozen=True)
class MeasurementNoise:
    """Synthetic sensor error. Deterministic for a given seed."""

    relative_sigma: float = 0.01
    absolute_sigma_mw: float = 0.004
    seed: int = 0

    def __post_init__(self):
        number_in_range("relative_sigma", self.relative_sigma, 0, 0.5)
        number_in_range("absolute_sigma_mw", self.absolute_sigma_mw, 0, 1)
        if type(self.seed) is not int:
            raise ValueError("seed must be an integer")


def synthesize_measurement(
    truth: FleetResult, noise: MeasurementNoise = MeasurementNoise(),
) -> tuple[float, ...]:
    """Per-block AC as a synthetic SCADA feed would report it.

    Not real data. `truth` must be a fleet run under the state you are
    pretending the physical plant is in, faults and all.
    """
    # Seeded per timestamp so the same instant always yields the same reading,
    # however many times it is requested, while different hours differ. crc32
    # rather than hash(): Python randomises string hashing per process, so
    # hash() would give a different "measurement" on every restart.
    generator = random.Random(zlib.crc32(f"{noise.seed}|{truth.timestamp_utc}".encode()))
    readings = []
    for block in truth.blocks:
        value = block.inverter_ac_mw
        if value > 0:
            value *= 1 + generator.gauss(0, noise.relative_sigma)
        value += generator.gauss(0, noise.absolute_sigma_mw)
        readings.append(max(0.0, value))
    return tuple(readings)


@dataclass(frozen=True)
class BlockResidual:
    block_id: str
    measured_ac_mw: float
    modelled_ac_mw: float
    residual_mw: float
    ratio: float | None
    relative_to_peers: float | None
    estimated_shortfall_mw: float
    flag: str


@dataclass(frozen=True)
class ResidualReport:
    """What the twin sees when it holds the model up against the measurement."""

    timestamp_utc: str
    source: str
    blocks: tuple[BlockResidual, ...]
    measured_plant_ac_mw: float
    modelled_plant_ac_mw: float
    plant_residual_mw: float
    fleet_median_ratio: float | None
    tolerance: float
    flagged_block_ids: tuple[str, ...]
    estimated_total_shortfall_mw: float
    warnings: tuple[str, ...]
    scope: str = "Anomaly detection by peer comparison; symptoms only, no diagnosis"
    assumptions: tuple[str, ...] = (
        "measurement_is_synthetic_generated_from_a_declared_true_state_not_real_telemetry",
        "peer_comparison_cannot_see_a_fault_that_affects_every_block_equally",
        "noise_is_independent_gaussian_per_block_real_sensor_error_is_correlated_and_drifts",
        "flags_are_instantaneous_no_persistence_or_duration_ranking",
        "blocks_below_a_minimum_output_are_not_flagged_because_sensor_noise_dominates_there",
        "detection_sensitivity_collapses_while_inverters_clip_around_solar_noon",
    )

    def block(self, block_id: str) -> BlockResidual:
        for block in self.blocks:
            if block.block_id == block_id:
                return block
        raise KeyError(f"No such block: {block_id!r}")


def compare_to_model(
    measured_block_ac_mw: tuple[float, ...] | list[float],
    model: FleetResult,
    tolerance: float = DEFAULT_TOLERANCE,
    source: str = "synthetic_measurement",
) -> ResidualReport:
    """Hold each measured block against what the twin expected of it."""
    if len(measured_block_ac_mw) != len(model.blocks):
        raise ValueError(f"Expected {len(model.blocks)} measurements, got {len(measured_block_ac_mw)}")
    number_in_range("tolerance", tolerance, 0.001, 1)
    for value in measured_block_ac_mw:
        number_in_range("measured_ac_mw", value, 0, 1e4)

    warnings = []
    pairs = list(zip(measured_block_ac_mw, model.blocks))
    producing = [(m, b) for m, b in pairs if b.inverter_ac_mw > 1e-6]
    ratios = [m / b.inverter_ac_mw for m, b in producing]

    median_ratio = None
    if len(ratios) >= MINIMUM_PEERS:
        median_ratio = statistics.median(ratios)
    elif not ratios:
        warnings.append("no_block_is_modelled_as_producing_residuals_undefined_in_darkness")
    else:
        warnings.append("too_few_producing_blocks_for_a_meaningful_peer_median")

    residuals = []
    for measured, block in pairs:
        modelled = block.inverter_ac_mw
        ratio = measured / modelled if modelled > 1e-6 else None
        relative = None
        flag = NORMAL
        shortfall = 0.0

        if ratio is None:
            flag = NORMAL
        elif median_ratio is None or median_ratio <= 0:
            flag = NORMAL
        elif modelled < MINIMUM_FLAGGABLE_MW:
            relative = ratio / median_ratio - 1
            flag = NORMAL
        else:
            relative = ratio / median_ratio - 1
            # What this block should have made if it behaved like its peers.
            expected = modelled * median_ratio
            if measured <= max(1e-6, NOT_PRODUCING_FRACTION * expected):
                flag = NOT_PRODUCING
                shortfall = expected
            elif relative < -tolerance:
                flag = UNDERPERFORMING
                shortfall = max(0.0, expected - measured)
            elif relative > tolerance:
                flag = ABOVE_MODEL

        residuals.append(BlockResidual(
            block_id=block.block_id,
            measured_ac_mw=measured,
            modelled_ac_mw=modelled,
            residual_mw=measured - modelled,
            ratio=ratio,
            relative_to_peers=relative,
            estimated_shortfall_mw=shortfall,
            flag=flag,
        ))

    flagged = tuple(r.block_id for r in residuals if r.flag in (UNDERPERFORMING, NOT_PRODUCING))
    if flagged:
        warnings.append("one_or_more_blocks_differ_from_their_peers_beyond_tolerance")
    if ratios and all(block.inverter_ac_mw < MINIMUM_FLAGGABLE_MW for block in model.blocks):
        warnings.append("output_too_low_for_peer_comparison_noise_dominates_at_this_light_level")
    if any(block.clipping_loss_mw > 1e-6 for block in model.blocks):
        warnings.append("model_is_clipping_so_shortfalls_smaller_than_the_clipped_headroom_are_invisible_now")
    if median_ratio is not None and abs(median_ratio - 1) > 0.10:
        warnings.append("fleet_median_is_far_from_the_model_a_plant_wide_input_or_model_error_is_likely")

    return ResidualReport(
        timestamp_utc=model.timestamp_utc,
        source=source,
        blocks=tuple(residuals),
        measured_plant_ac_mw=sum(measured_block_ac_mw),
        modelled_plant_ac_mw=model.plant_inverter_ac_mw,
        plant_residual_mw=sum(measured_block_ac_mw) - model.plant_inverter_ac_mw,
        fleet_median_ratio=median_ratio,
        tolerance=tolerance,
        flagged_block_ids=flagged,
        estimated_total_shortfall_mw=sum(r.estimated_shortfall_mw for r in residuals),
        warnings=tuple(dict.fromkeys(warnings)),
    )
