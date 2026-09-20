"""Step 9: dust accumulation between rain events.

Pavagada is semi-arid. Dust settles on the glass every dry day and is washed
off when it rains hard enough, so the array's optical loss is a sawtooth
driven entirely by weather history - which makes it the first quantity in
this project that depends on *what happened before now*, not just on the
current instant. Everything up to Step 8 was a pure function of one timestamp.

Model: Kimber et al. (2014), "The Effect of Soiling on Large Grid-Connected
Photovoltaic Systems in California and the Southwest Region of the United
States", the same model as `pvlib.soiling.kimber`. Soiling accrues linearly
at a fixed rate per dry day; rainfall above a threshold within a 24 h window
resets it to zero; for a grace period afterwards the surface stays clean.

Honest limits:

* The deposition rate is a site constant here. Real deposition varies with
  wind, humidity, agricultural activity and traffic on site roads, none of
  which are modeled. The default is pvlib's, which is a US Southwest figure;
  measured Indian semi-arid rates are often several times higher, so this is
  a conservative floor, not a Pavagada-specific measurement.
* Cleaning is all-or-nothing at the threshold. Real partial rain leaves
  streaks and can briefly make things worse.
* Soiling is treated as a uniform optical loss, the same way `dc.simulate_dc`
  already treats `soiling_loss_fraction`. Non-uniform dust causes electrical
  mismatch between cells; that is not modeled.
* Scheduled manual washing is not modeled.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from .solar_position import _require_utc
from .validation import number_in_range


@dataclass(frozen=True)
class SoilingParameters:
    deposition_rate_per_day: float = 0.0015
    cleaning_threshold_mm: float = 6.0
    grace_period_days: float = 14.0
    maximum_soiling_fraction: float = 0.3
    rain_accumulation_hours: int = 24

    def __post_init__(self):
        number_in_range("deposition_rate_per_day", self.deposition_rate_per_day, 0, 0.1)
        number_in_range("cleaning_threshold_mm", self.cleaning_threshold_mm, 0.1, 100)
        number_in_range("grace_period_days", self.grace_period_days, 0, 365)
        number_in_range("maximum_soiling_fraction", self.maximum_soiling_fraction, 0, 0.9)
        if type(self.rain_accumulation_hours) is not int:
            raise ValueError("rain_accumulation_hours must be an integer")
        number_in_range("rain_accumulation_hours", self.rain_accumulation_hours, 1, 168)


@dataclass(frozen=True)
class SoilingPoint:
    timestamp_utc: datetime
    soiling_loss_fraction: float
    rain_in_window_mm: float
    cleaned: bool
    dry_days: float


@dataclass(frozen=True)
class SoilingSeries:
    points: tuple[SoilingPoint, ...]
    parameters: SoilingParameters
    cleaning_events: int
    final_soiling_loss_fraction: float
    warnings: tuple[str, ...]
    scope: str = "Uniform optical soiling loss from rainfall history; mismatch and manual washing are not modeled"
    assumptions: tuple[str, ...] = (
        "deposition_rate_is_a_site_constant_not_a_measured_pavagada_value",
        "cleaning_is_all_or_nothing_at_the_rainfall_threshold",
        "soiling_applied_uniformly_across_the_array_cell_level_mismatch_not_modeled",
        "scheduled_manual_module_washing_not_modeled",
    )


def soiling_after_dry_days(dry_days: float, parameters: SoilingParameters = SoilingParameters()) -> float:
    """Standalone estimate: optical loss after a given unbroken dry spell."""
    number_in_range("dry_days", dry_days, 0, 10000)
    return min(parameters.maximum_soiling_fraction, parameters.deposition_rate_per_day * dry_days)


def simulate_soiling(
    timestamps: list[datetime],
    precipitation_mm: list[float],
    parameters: SoilingParameters = SoilingParameters(),
    initial_soiling_fraction: float = 0.0,
    initial_dry_days: float = 0.0,
) -> SoilingSeries:
    """Walk an hourly rainfall history forward into a soiling loss series.

    `initial_soiling_fraction` carries state in from a previous run, so a long
    history can be simulated in chunks without restarting from clean glass.
    """
    if len(timestamps) != len(precipitation_mm):
        raise ValueError("timestamps and precipitation_mm must be the same length")
    if not timestamps:
        raise ValueError("At least one timestamp is required")
    number_in_range("initial_soiling_fraction", initial_soiling_fraction, 0,
                    parameters.maximum_soiling_fraction)
    number_in_range("initial_dry_days", initial_dry_days, 0, 10000)
    for timestamp in timestamps:
        _require_utc(timestamp)
    for rain in precipitation_mm:
        number_in_range("precipitation_mm", rain, 0, 500)

    warnings = []
    window = timedelta(hours=parameters.rain_accumulation_hours)
    grace = timedelta(days=parameters.grace_period_days)
    soiling, dry_days = initial_soiling_fraction, initial_dry_days
    cleaned_at: datetime | None = None
    cleaning_events, was_cleaning = 0, False
    points = []

    for index, (now, rain) in enumerate(zip(timestamps, precipitation_mm)):
        if index and now <= timestamps[index - 1]:
            raise ValueError("timestamps must be strictly increasing")
        elapsed_hours = ((now - timestamps[index - 1]).total_seconds() / 3600) if index else 0.0

        # Rain inside the trailing accumulation window, not just this hour.
        rain_in_window, cursor = 0.0, index
        while cursor >= 0 and now - timestamps[cursor] < window:
            rain_in_window += precipitation_mm[cursor]
            cursor -= 1

        cleaned = rain_in_window >= parameters.cleaning_threshold_mm
        if cleaned:
            soiling, dry_days, cleaned_at = 0.0, 0.0, now
            # One rainfall event stays inside the trailing window for many
            # hours; count the storm, not each hour it is still remembered.
            if not was_cleaning:
                cleaning_events += 1
        elif cleaned_at is not None and now - cleaned_at < grace:
            soiling, dry_days = 0.0, 0.0
        else:
            dry_days += elapsed_hours / 24
            soiling = min(parameters.maximum_soiling_fraction,
                          soiling + parameters.deposition_rate_per_day * elapsed_hours / 24)

        was_cleaning = cleaned
        points.append(SoilingPoint(now, soiling, rain_in_window, cleaned, dry_days))

    if points[-1].soiling_loss_fraction >= parameters.maximum_soiling_fraction:
        warnings.append("soiling_pinned_at_configured_maximum_longer_dry_spells_are_not_distinguished")
    if cleaning_events == 0:
        warnings.append("no_rainfall_reached_the_cleaning_threshold_in_this_window")
    span_days = (timestamps[-1] - timestamps[0]).total_seconds() / 86400
    if span_days < parameters.grace_period_days and initial_soiling_fraction == 0:
        warnings.append("history_shorter_than_the_grace_period_soiling_estimate_is_weakly_constrained")

    return SoilingSeries(
        points=tuple(points),
        parameters=parameters,
        cleaning_events=cleaning_events,
        final_soiling_loss_fraction=points[-1].soiling_loss_fraction,
        warnings=tuple(warnings),
    )
