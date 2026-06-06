"""Tests for ``furnace.prediction_cache`` — the Conduit prediction-cache
producer (PRD slice #70).

A real cohort is trained on the committed BTC kline slice via the shared
``cohort_round`` / ``fake_spot_klines`` / ``frozen_klines`` fixtures
(``conftest.py``). ``get_arrow_file`` is patched only for the cohort's *input*
series read, so ``predict_all`` runs on schema-matching klines offline while the
cache file ``backfill`` writes is still read back through the genuine zero-copy
reader (the single-batch/uncompressed proof).

The fixed-width envelope and its two invariants are covered directly: ``ts`` is
the exact UTC epoch-ns of the bar, and an unmapped reason fails loud rather than
being mislabelled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest
from limen.data import historical_data

from furnace import prediction_cache

_SERIES = "time_test"


def _serve_arrow(monkeypatch, klines: pl.DataFrame, *, arrow_root: Path, series: str) -> None:
    """Serve the in-memory slice for the cohort's *input* series read, but let
    every other path — notably the cache file written under ``conduit_root`` —
    hit the real ``get_arrow_file``, so a "zero-copy readable" assertion
    validates the file ``backfill`` actually wrote rather than the patch."""
    original = historical_data.HistoricalData.get_arrow_file
    input_path = str(arrow_root / series / "latest.arrow")

    def fake(self, file_path, *args, **kwargs):
        if str(file_path) == input_path:
            return klines
        return original(self, file_path, *args, **kwargs)

    monkeypatch.setattr(historical_data.HistoricalData, "get_arrow_file", fake)


class _Bar:
    """A minimal BarPrediction stand-in for unit-testing ``_to_frame`` directly
    (the attributes ``_to_frame`` reads off a limen ``BarPrediction``)."""

    def __init__(self, when: datetime | None, prediction, probability, reason) -> None:
        self.datetime = when
        self.prediction = prediction
        self.probability = probability
        self.reason = reason


def test_backfill_writes_single_batch_uncompressed_arrow(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """backfill writes a file the Limen zero-copy reader accepts — single-batch
    uncompressed Arrow IPC. ``get_arrow_file``'s ``_validate_arrow_zero_copy``
    raises on a compressed or multi-batch file, so a clean read of the *written*
    file (not the patched input) is the proof."""
    round_dir, rids, _at = cohort_round
    _serve_arrow(monkeypatch, frozen_klines, arrow_root=tmp_path / "arrow", series=_SERIES)
    cohort_def = {"experiment_dir": str(round_dir), "permutation_ids": list(rids)}

    out = prediction_cache.backfill(
        cohort_def, _SERIES, conduit_root=tmp_path / "conduit", arrow_root=tmp_path / "arrow"
    )
    # the genuine Limen reader validates single-batch zero-copy IPC on the written file
    validated = historical_data.HistoricalData().get_arrow_file(str(out))
    assert validated.height > 0


def test_cache_columns_and_ts_sorted(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The written frame carries exactly the fixed-width envelope, ts-sorted."""
    round_dir, rids, _at = cohort_round
    _serve_arrow(monkeypatch, frozen_klines, arrow_root=tmp_path / "arrow", series=_SERIES)
    cohort_def = {"experiment_dir": str(round_dir), "permutation_ids": list(rids)}

    out = prediction_cache.backfill(
        cohort_def, _SERIES, conduit_root=tmp_path / "conduit", arrow_root=tmp_path / "arrow"
    )
    df = pl.read_ipc(out, memory_map=True)
    assert df.columns == ["ts", "prediction", "probability", "reason_code"]
    assert df.schema["ts"] == pl.Int64
    assert df.schema["reason_code"] == pl.Int8
    assert df.get_column("ts").is_sorted()
    assert df.height > 0


def test_cache_path_layout(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The cache lands at ``<conduit_root>/<series>/latest.arrow``."""
    round_dir, rids, _at = cohort_round
    _serve_arrow(monkeypatch, frozen_klines, arrow_root=tmp_path / "arrow", series=_SERIES)
    cohort_def = {"experiment_dir": str(round_dir), "permutation_ids": list(rids)}
    conduit = tmp_path / "conduit"

    out = prediction_cache.backfill(
        cohort_def, _SERIES, conduit_root=conduit, arrow_root=tmp_path / "arrow"
    )
    assert out == conduit / _SERIES / "latest.arrow"
    assert out.is_file()


def test_ts_is_exact_epoch_ns_not_float_rounded() -> None:
    """``ts`` is the exact UTC epoch-ns of the bar (polars ``dt.epoch``), so a
    consumer joins predictions to the canonical klines by ``ts`` without the
    ~100ns drift the float ``timestamp() * 1e9`` path would introduce."""
    when = datetime(2024, 5, 1, 12, 0, 0, tzinfo=UTC)
    exact = pl.DataFrame({"d": [when]}).select(pl.col("d").dt.epoch("ns")).item()
    frame = prediction_cache._to_frame([_Bar(when, 1, 0.6, None)])
    assert frame.get_column("ts").to_list() == [exact]


def test_undated_bars_are_dropped() -> None:
    """A bar with no datetime (sensor-error / all-members-errored) has no
    addressable ``ts``; both write paths drop it, so ``top_up``'s ts-keyed
    ``unique`` can never collapse a column of null-ts rows into one."""
    when = datetime(2024, 5, 1, 12, 0, 0, tzinfo=UTC)
    frame = prediction_cache._to_frame(
        [_Bar(when, 1, 0.6, "warm-up"), _Bar(None, None, None, "sensor-error")]
    )
    assert frame.height == 1
    assert frame.get_column("reason_code").to_list() == [1]


def test_unmapped_reason_fails_loud() -> None:
    """An unrecognised reason raises rather than being silently coerced to
    ``sensor-error`` — a reason a future limen adds must be mapped deliberately,
    not mislabelled in a surface a consumer reads as ground truth."""
    when = datetime(2024, 5, 1, 12, 0, 0, tzinfo=UTC)
    with pytest.raises(KeyError):
        prediction_cache._to_frame([_Bar(when, 1, 0.5, "a-reason-limen-does-not-have-yet")])
