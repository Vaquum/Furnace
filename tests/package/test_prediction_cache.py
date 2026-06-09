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

import shutil
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest
from limen.data import historical_data

from furnace import prediction_cache

_SERIES = "time_test"


def _isolated_round(round_dir: Path, dest: Path) -> Path:
    """Copy the session round to a per-test dir and strip any persisted sensor
    store, so the warm-up/persistence assertions start from a cold store
    regardless of which other test populated the shared ``cohort_round`` first."""
    shutil.copytree(round_dir, dest)
    sensors = dest / "sensors"
    if sensors.exists():
        shutil.rmtree(sensors)
    return dest


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


def test_backfill_persists_sensors(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The first backfill trains each member once and persists it to the
    per-experiment store (``<experiment_dir>/sensors/<pid>.pkl`` + ``.json``),
    so a later tick can reload it instead of re-training."""
    round_dir, rids, _at = cohort_round
    exp = _isolated_round(round_dir, tmp_path / "round")
    arrow = tmp_path / "arrow"
    _serve_arrow(monkeypatch, frozen_klines, arrow_root=arrow, series=_SERIES)
    cohort_def = {"experiment_dir": str(exp), "permutation_ids": list(rids)}

    prediction_cache.backfill(cohort_def, _SERIES, conduit_root=tmp_path / "conduit", arrow_root=arrow)

    store = exp / "sensors"
    for pid in rids:
        assert (store / f"{pid}.pkl").is_file()
        assert (store / f"{pid}.json").is_file()


def test_topup_reuses_persisted_sensors_without_retrain(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A warm top_up reloads the persisted sensors and never calls
    ``Trainer.train`` — the per-tick re-train that made the refresh O(history)
    is gone once the store is warm."""
    round_dir, rids, _at = cohort_round
    exp = _isolated_round(round_dir, tmp_path / "round")
    cohort_def = {"experiment_dir": str(exp), "permutation_ids": list(rids)}
    conduit, arrow = tmp_path / "conduit", tmp_path / "arrow"

    calls: list[list[str]] = []
    real_train = prediction_cache.Trainer.train

    def spy_train(self, permutation_ids, *a, **k):
        calls.append(list(permutation_ids))
        return real_train(self, permutation_ids, *a, **k)

    monkeypatch.setattr(prediction_cache.Trainer, "train", spy_train)

    head = frozen_klines.head(int(frozen_klines.height * 0.8))
    _serve_arrow(monkeypatch, head, arrow_root=arrow, series=_SERIES)
    prediction_cache.backfill(cohort_def, _SERIES, conduit, arrow)
    assert calls, "a cold backfill must train the members at least once"
    calls.clear()

    _serve_arrow(monkeypatch, frozen_klines, arrow_root=arrow, series=_SERIES)
    appended = prediction_cache.top_up(cohort_def, _SERIES, conduit, arrow)
    assert appended > 0
    assert calls == [], "a warm top_up must reuse persisted sensors, never re-train"


def test_topup_scores_bounded_window_not_full_history(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """top_up scores a bounded tail (``WARMUP_BARS + n_new`` bars), not the
    whole history — proven by capturing the frame handed to ``predict_all`` with
    a small WARMUP_BARS so the window is a strict subset of the series."""
    round_dir, rids, _at = cohort_round
    exp = _isolated_round(round_dir, tmp_path / "round")
    cohort_def = {"experiment_dir": str(exp), "permutation_ids": list(rids)}
    conduit, arrow = tmp_path / "conduit", tmp_path / "arrow"

    monkeypatch.setattr(prediction_cache, "WARMUP_BARS", 100)

    heights: list[int] = []
    real_predict_all = prediction_cache.Cohort.predict_all

    def spy_predict_all(self, frame, *a, **k):
        heights.append(frame.height)
        return real_predict_all(self, frame, *a, **k)

    monkeypatch.setattr(prediction_cache.Cohort, "predict_all", spy_predict_all)

    head = frozen_klines.head(int(frozen_klines.height * 0.8))
    _serve_arrow(monkeypatch, head, arrow_root=arrow, series=_SERIES)
    prediction_cache.backfill(cohort_def, _SERIES, conduit, arrow)

    _serve_arrow(monkeypatch, frozen_klines, arrow_root=arrow, series=_SERIES)
    n_new = frozen_klines.height - head.height
    prediction_cache.top_up(cohort_def, _SERIES, conduit, arrow)

    assert heights[-1] == 100 + n_new
    assert heights[-1] < frozen_klines.height  # a bounded window, not the full history


def test_windowed_topup_matches_full_backfill(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A genuinely *truncated* top_up window reproduces a full backfill's rows
    byte-for-byte. WARMUP_BARS is lowered to 1100 so the window is
    ``tail(1100 + n_new)`` = 1417 of the 1585-bar fixture — a strict subset that
    still covers the feature warm-up — yet the appended rows are byte-identical
    to a full backfill's. A window too short to cover the warm-up drifts (the
    causal rolling scaler's stats change with the leading context), so this is
    the guard the WARMUP_BARS margin rests on; pinning the test at the full 2000
    would only compare the whole history to itself."""
    round_dir, rids, _at = cohort_round
    exp = _isolated_round(round_dir, tmp_path / "round")
    cohort_def = {"experiment_dir": str(exp), "permutation_ids": list(rids)}
    arrow = tmp_path / "arrow"
    conduit_topup, conduit_full = tmp_path / "conduit_topup", tmp_path / "conduit_full"

    # Reference: one full backfill over the entire real history.
    _serve_arrow(monkeypatch, frozen_klines, arrow_root=arrow, series=_SERIES)
    prediction_cache.backfill(cohort_def, _SERIES, conduit_full, arrow)
    full = pl.read_ipc(prediction_cache.cache_path(_SERIES, conduit_full), memory_map=True)

    # Incremental: backfill a head, then top up the withheld real tail.
    head = frozen_klines.head(int(frozen_klines.height * 0.8))
    _serve_arrow(monkeypatch, head, arrow_root=arrow, series=_SERIES)
    prediction_cache.backfill(cohort_def, _SERIES, conduit_topup, arrow)
    last_ts = pl.read_ipc(
        prediction_cache.cache_path(_SERIES, conduit_topup), memory_map=True
    ).get_column("ts").max()

    # Bound the top-up to a strict subset and capture the scored frame's height
    # to prove it really truncated (1100 + n_new < the full history).
    monkeypatch.setattr(prediction_cache, "WARMUP_BARS", 1100)
    scored_heights: list[int] = []
    real_predict_all = prediction_cache.Cohort.predict_all

    def spy_predict_all(self, frame, *a, **k):
        scored_heights.append(frame.height)
        return real_predict_all(self, frame, *a, **k)

    monkeypatch.setattr(prediction_cache.Cohort, "predict_all", spy_predict_all)
    _serve_arrow(monkeypatch, frozen_klines, arrow_root=arrow, series=_SERIES)
    prediction_cache.top_up(cohort_def, _SERIES, conduit_topup, arrow)
    topped = pl.read_ipc(prediction_cache.cache_path(_SERIES, conduit_topup), memory_map=True)

    n_new = frozen_klines.height - head.height
    assert scored_heights == [1100 + n_new]           # the top-up scored a bounded tail...
    assert 1100 + n_new < frozen_klines.height         # ...that is a strict subset of the history
    new_from_topup = topped.filter(pl.col("ts") > last_ts).sort("ts")
    new_from_full = full.filter(pl.col("ts") > last_ts).sort("ts")
    assert new_from_topup.height > 0
    assert new_from_topup.equals(new_from_full)         # byte-identical despite the truncation


def test_topup_no_new_bar_skips_bind_and_predict(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """When no bar has closed since the last tick, top_up returns 0 without
    binding a cohort (and so without predicting) — an idle tick is a cheap ts
    comparison, not a re-score."""
    round_dir, rids, _at = cohort_round
    exp = _isolated_round(round_dir, tmp_path / "round")
    cohort_def = {"experiment_dir": str(exp), "permutation_ids": list(rids)}
    conduit, arrow = tmp_path / "conduit", tmp_path / "arrow"

    _serve_arrow(monkeypatch, frozen_klines, arrow_root=arrow, series=_SERIES)
    prediction_cache.backfill(cohort_def, _SERIES, conduit, arrow)

    def boom(*_a, **_k):
        raise AssertionError("top_up bound a cohort despite no new bar")

    monkeypatch.setattr(prediction_cache, "_bound_cohort", boom)

    # The series carries no bar newer than the cache's last ts.
    assert prediction_cache.top_up(cohort_def, _SERIES, conduit, arrow) == 0
