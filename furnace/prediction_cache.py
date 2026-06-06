"""Conduit prediction cache (PRD slice #70).

Materialize a cohort's ``BarPrediction``s over a canonical bar series and write
them as a **single-batch uncompressed** Arrow file to
``/opt/conduit/<series>/latest.arrow`` — the zero-copy serving surface a
co-located consumer mmaps. ``backfill`` writes the full history; ``top_up``
appends only bars newer than the cache's last ``ts`` (deterministic +
idempotent), the per-minute path used by the scheduler.

The on-disk envelope is fixed-width so the file is zero-copy mmappable:
``ts`` (Int64, UTC epoch ns), ``prediction`` (Int8), ``probability`` (Float64),
``reason_code`` (Int8: 0=ok 1=warm-up 2=inside-training-window 3=null-features
4=sensor-error).

Limen's ``Cohort`` / ``HistoricalData`` / ``Trainer`` are imported from
``furnace.inference`` rather than from ``limen`` directly: inference.py is the
module that already crosses the limen boundary, and re-using its imports keeps
this module free of the stub-less ``from limen …`` statements that the frozen
pyright budget has no room to absorb.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import polars as pl

from furnace.inference import Cohort, HistoricalData, Trainer

CONDUIT_ROOT = Path("/opt/conduit")
ARROW_ROOT = Path("/opt/arrow")

_REASON_CODE: dict[str | None, int] = {
    None: 0,
    "warm-up": 1,
    "inside-training-window": 2,
    "null-features": 3,
    "sensor-error": 4,
}

_SCHEMA: dict[str, pl.DataType] = {
    "ts": pl.Int64(),
    "prediction": pl.Int8(),
    "probability": pl.Float64(),
    "reason_code": pl.Int8(),
}


def cache_path(series: str, conduit_root: Path = CONDUIT_ROOT) -> Path:
    """The serving file for one canonical series: ``<root>/<series>/latest.arrow``."""
    return conduit_root / series / "latest.arrow"


def _experiment_dir(cohort_def: dict[str, object]) -> str:
    value = cohort_def.get("experiment_dir")
    if not isinstance(value, str) or not value:
        raise ValueError(f"cohort_def['experiment_dir'] must be a non-empty string; got {value!r}")
    return value


def _permutation_ids(cohort_def: dict[str, object]) -> list[str]:
    raw = cohort_def.get("permutation_ids")
    if not isinstance(raw, list):
        raise ValueError("cohort_def['permutation_ids'] must be a list")
    items = cast(list[object], raw)
    ids = [p for p in items if isinstance(p, str)]
    if len(ids) != len(items) or not ids:
        raise ValueError("cohort_def['permutation_ids'] must be a non-empty list of strings")
    return ids


def _bound_cohort(cohort_def: dict[str, object]) -> Cohort:
    """Trainer.train materializes the member sensors; bind them into a Cohort.
    Persistence (sensor_registry) is an optimization layered later, not needed
    to produce the cache."""
    experiment_dir = _experiment_dir(cohort_def)
    permutation_ids = _permutation_ids(cohort_def)
    sensors = Trainer(experiment_dir).train(permutation_ids)
    cohort = Cohort(experiment_log_path=experiment_dir, permutation_ids=list(permutation_ids))
    cohort.set_members(sensors)
    return cohort


def _series_klines(series: str, arrow_root: Path) -> pl.DataFrame:
    """Zero-copy read of the canonical bar series the cohort predicts over."""
    return HistoricalData().get_arrow_file(str(arrow_root / series / "latest.arrow"))


def _to_frame(bars: list[object]) -> pl.DataFrame:
    """BarPredictions -> the fixed-width PRD frame, ts-sorted."""
    rows: list[tuple[int | None, int | float | None, float | None, int]] = []
    for bar in bars:
        when = getattr(bar, "datetime")
        ts = int(when.timestamp() * 1_000_000_000) if when is not None else None
        reason = getattr(bar, "reason")
        code = _REASON_CODE.get(reason if isinstance(reason, str) else None, 4)
        rows.append((ts, getattr(bar, "prediction"), getattr(bar, "probability"), code))
    return pl.DataFrame(rows, schema=_SCHEMA, orient="row").sort("ts").rechunk()


def _write_atomic(frame: pl.DataFrame, path: Path) -> None:
    """Single-batch uncompressed Arrow IPC (the zero-copy requirement), written
    atomically so a reader never sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    frame.rechunk().write_ipc(tmp, compression="uncompressed")
    tmp.replace(path)


def backfill(
    cohort_def: dict[str, object],
    series: str,
    conduit_root: Path = CONDUIT_ROOT,
    arrow_root: Path = ARROW_ROOT,
) -> Path:
    """Predict the cohort over the full series history and write the cache.
    Returns the written path (``<conduit_root>/<series>/latest.arrow``)."""
    cohort = _bound_cohort(cohort_def)
    klines = _series_klines(series, arrow_root)
    frame = _to_frame(list(cohort.predict_all(klines)))
    path = cache_path(series, conduit_root)
    _write_atomic(frame, path)
    return path


def top_up(
    cohort_def: dict[str, object],
    series: str,
    conduit_root: Path = CONDUIT_ROOT,
    arrow_root: Path = ARROW_ROOT,
) -> int:
    """Append bars newer than the cache's last ``ts``. Deterministic and
    idempotent — re-running a minute appends nothing (a closed bar's prediction
    never changes), so a missed tick self-corrects on the next run. Returns the
    number of bars appended. Falls back to a full ``backfill`` when no cache
    exists yet."""
    path = cache_path(series, conduit_root)
    if not path.exists():
        backfill(cohort_def, series, conduit_root, arrow_root)
        return pl.read_ipc(path, memory_map=True).height
    existing = pl.read_ipc(path, memory_map=True)
    last_ts = existing.get_column("ts").max() if existing.height else None

    cohort = _bound_cohort(cohort_def)
    fresh = _to_frame(list(cohort.predict_all(_series_klines(series, arrow_root))))
    new_rows = fresh.filter(pl.col("ts") > last_ts) if last_ts is not None else fresh
    if not new_rows.height:
        return 0
    combined = pl.concat([existing, new_rows]).unique(subset=["ts"], keep="last").sort("ts")
    _write_atomic(combined, path)
    return new_rows.height
