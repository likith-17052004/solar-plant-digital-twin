"""Step 12b: remember. A twin that forgets yesterday cannot spot a trend.

Every earlier step is a pure function: ask the same question twice and get the
same answer, with nothing kept in between. That is why the model could say
"block 7 is 4% low" but never "block 7 has been 4% low for nine days" - and
the second sentence is the one an operator acts on.

Storage is `sqlite3`, which is in the Python standard library, so this adds
persistence without breaking the project's no-dependency rule.

Three tables, each answering a question the stateless model cannot:

* `daily_energy`  - how has yield and PR moved over weeks and months?
* `block_daily`   - which block is persistently behind its peers?
* `fault_log`     - when did this start, and is it still going?

Honest limits:

* This stores *simulated* results. It is a record of what the model said, not
  a historian of a real plant, and re-running a day with better weather data
  legitimately overwrites the old row (writes are upserts on the natural key).
* Soiling is the one genuinely path-dependent quantity, and it is recomputed
  from rainfall rather than read back from here; this layer records outcomes,
  it is not an authority the physics reads from.
* No migrations. The schema is created if absent; changing it means deleting
  the file. Fine for a single-user local twin, not for anything shared.
* Times are stored as ISO-8601 UTC strings, which sort correctly but carry no
  timezone handling of their own.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_energy (
    local_date            TEXT PRIMARY KEY,
    weather_source        TEXT NOT NULL,
    poa_irradiation_kwh_m2 REAL NOT NULL,
    export_energy_mwh     REAL NOT NULL,
    performance_ratio     REAL,
    specific_yield_kwh_per_kwp REAL NOT NULL,
    peak_export_mw        REAL NOT NULL,
    mean_soiling_fraction REAL NOT NULL,
    recorded_utc          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS block_daily (
    local_date        TEXT NOT NULL,
    block_id          TEXT NOT NULL,
    export_energy_mwh REAL NOT NULL,
    flagged_hours     REAL NOT NULL DEFAULT 0,
    worst_relative_to_peers REAL,
    PRIMARY KEY (local_date, block_id)
);

CREATE TABLE IF NOT EXISTS fault_log (
    block_id    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    severity    REAL NOT NULL,
    started_utc TEXT NOT NULL,
    ended_utc   TEXT,
    note        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (block_id, kind, started_utc)
);

CREATE INDEX IF NOT EXISTS block_daily_by_block ON block_daily (block_id, local_date);
CREATE INDEX IF NOT EXISTS fault_log_open ON fault_log (ended_utc, block_id);
"""


@dataclass(frozen=True)
class BlockTrend:
    """One block's recent behaviour, which is what makes a flag actionable."""

    block_id: str
    days: int
    days_flagged: int
    mean_relative_to_peers: float | None
    worst_relative_to_peers: float | None
    total_export_mwh: float
    consecutive_days_flagged: int


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware UTC")
    return value.astimezone(timezone.utc).isoformat()


class History:
    """A SQLite-backed record of what the twin has seen."""

    def __init__(self, path: str | Path = "history.sqlite3"):
        self.path = str(path)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    # -- writes ---------------------------------------------------------

    def record_day(self, local_date: str, weather_source: str, totals, mean_soiling_fraction: float) -> None:
        """Upsert one day's plant-level energy summary."""
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO daily_energy VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(local_date) DO UPDATE SET
                     weather_source=excluded.weather_source,
                     poa_irradiation_kwh_m2=excluded.poa_irradiation_kwh_m2,
                     export_energy_mwh=excluded.export_energy_mwh,
                     performance_ratio=excluded.performance_ratio,
                     specific_yield_kwh_per_kwp=excluded.specific_yield_kwh_per_kwp,
                     peak_export_mw=excluded.peak_export_mw,
                     mean_soiling_fraction=excluded.mean_soiling_fraction,
                     recorded_utc=excluded.recorded_utc""",
                (local_date, weather_source, totals.poa_irradiation_kwh_m2,
                 totals.export_energy_mwh, totals.performance_ratio,
                 totals.specific_yield_kwh_per_kwp, totals.peak_export_mw,
                 mean_soiling_fraction, _iso(datetime.now(timezone.utc))),
            )

    def record_block_day(
        self, local_date: str, block_id: str, export_energy_mwh: float,
        flagged_hours: float = 0.0, worst_relative_to_peers: float | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO block_daily VALUES (?,?,?,?,?)
                   ON CONFLICT(local_date, block_id) DO UPDATE SET
                     export_energy_mwh=excluded.export_energy_mwh,
                     flagged_hours=excluded.flagged_hours,
                     worst_relative_to_peers=excluded.worst_relative_to_peers""",
                (local_date, block_id, export_energy_mwh, flagged_hours, worst_relative_to_peers),
            )

    def open_fault(self, block_id: str, kind: str, severity: float,
                   started_utc: datetime, note: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO fault_log VALUES (?,?,?,?,NULL,?)",
                (block_id, kind, severity, _iso(started_utc), note),
            )

    def close_fault(self, block_id: str, kind: str, ended_utc: datetime) -> int:
        """Close any open fault of this kind on this block. Returns rows closed."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE fault_log SET ended_utc=? WHERE block_id=? AND kind=? AND ended_utc IS NULL",
                (_iso(ended_utc), block_id, kind),
            )
            return cursor.rowcount

    # -- reads ----------------------------------------------------------

    def daily_energy(self, limit: int = 90) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM daily_energy ORDER BY local_date DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def open_faults(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM fault_log WHERE ended_utc IS NULL ORDER BY started_utc"
            ).fetchall()
        return [dict(row) for row in rows]

    def fault_age_days(self, block_id: str, kind: str, as_of_utc: datetime) -> float | None:
        """How long an open fault has been running. The number that makes it urgent."""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT started_utc FROM fault_log
                   WHERE block_id=? AND kind=? AND ended_utc IS NULL
                   ORDER BY started_utc LIMIT 1""",
                (block_id, kind),
            ).fetchone()
        if row is None:
            return None
        started = datetime.fromisoformat(row["started_utc"])
        return max(0.0, (as_of_utc - started).total_seconds() / 86400)

    def block_trend(self, block_id: str, days: int = 30) -> BlockTrend:
        """Summarise one block's recent record. `days` counts stored days, not calendar days."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM block_daily WHERE block_id=?
                   ORDER BY local_date DESC LIMIT ?""",
                (block_id, days),
            ).fetchall()
        rows = list(reversed(rows))
        if not rows:
            return BlockTrend(block_id, 0, 0, None, None, 0.0, 0)

        relatives = [row["worst_relative_to_peers"] for row in rows
                     if row["worst_relative_to_peers"] is not None]
        consecutive = 0
        for row in reversed(rows):
            if row["flagged_hours"] > 0:
                consecutive += 1
            else:
                break
        return BlockTrend(
            block_id=block_id,
            days=len(rows),
            days_flagged=sum(1 for row in rows if row["flagged_hours"] > 0),
            mean_relative_to_peers=(sum(relatives) / len(relatives)) if relatives else None,
            worst_relative_to_peers=min(relatives) if relatives else None,
            total_export_mwh=sum(row["export_energy_mwh"] for row in rows),
            consecutive_days_flagged=consecutive,
        )

    def worst_blocks(self, days: int = 30, limit: int = 5) -> list[BlockTrend]:
        """Rank blocks by how persistently they have been behind their peers."""
        with self._connect() as connection:
            ids = [row["block_id"] for row in
                   connection.execute("SELECT DISTINCT block_id FROM block_daily").fetchall()]
        trends = [self.block_trend(block_id, days) for block_id in ids]
        ranked = [t for t in trends if t.days_flagged > 0]
        ranked.sort(key=lambda t: (-t.days_flagged, t.mean_relative_to_peers or 0))
        return ranked[:limit]
