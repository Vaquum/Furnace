"""Tests for ``furnace.prediction_cache`` — the Conduit prediction-cache
producer (PRD slice #70).

A real cohort is trained on the committed BTC kline slice via the shared
``cohort_round`` / ``fake_spot_klines`` / ``frozen_klines`` fixtures
(``conftest.py``). ``get_arrow_file`` (the canonical-series read inside
``backfill``) is patched to serve that same slice, so ``predict_all`` runs on
schema-matching klines, offline.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
from limen.data import historical_data

from furnace import prediction_cache

_SERIES = "time_test"


def _serve_arrow(monkeypatch, klines: pl.DataFrame) -> None:
    monkeypatch.setattr(
        historical_data.HistoricalData,
        "get_arrow_file",
        staticmethod(lambda *_a, **_k: klines),
    )


def test_backfill_writes_single_batch_uncompressed_arrow(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """backfill writes a file the Limen zero-copy reader accepts — single-batch
    uncompressed Arrow IPC. ``get_arrow_file``'s ``_validate_arrow_zero_copy``
    raises on a compressed or multi-batch file, so a clean read is the proof."""
    round_dir, rids, _at = cohort_round
    _serve_arrow(monkeypatch, frozen_klines)
    cohort_def = {"experiment_dir": str(round_dir), "permutation_ids": list(rids)}

    out = prediction_cache.backfill(
        cohort_def, _SERIES, conduit_root=tmp_path / "conduit", arrow_root=tmp_path / "arrow"
    )
    # the canonical Limen reader validates genuine single-batch zero-copy IPC
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
    _serve_arrow(monkeypatch, frozen_klines)
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
    _serve_arrow(monkeypatch, frozen_klines)
    cohort_def = {"experiment_dir": str(round_dir), "permutation_ids": list(rids)}
    conduit = tmp_path / "conduit"

    out = prediction_cache.backfill(cohort_def, _SERIES, conduit_root=conduit, arrow_root=tmp_path / "arrow")
    assert out == conduit / _SERIES / "latest.arrow"
    assert out.is_file()
