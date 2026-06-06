"""Tests for ``furnace.cache_scheduler`` — per-minute Conduit top-up (PRD #71).

Reuses the shared ``cohort_round`` / ``fake_spot_klines`` / ``frozen_klines``
fixtures (``conftest.py``); ``get_arrow_file`` is patched to serve a controlled
slice so a "new bar arrived" can be simulated deterministically.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from limen.data import historical_data

from furnace import prediction_cache

_SERIES = "time_test"


def _serve_arrow(monkeypatch: pytest.MonkeyPatch, klines: pl.DataFrame) -> None:
    monkeypatch.setattr(
        historical_data.HistoricalData,
        "get_arrow_file",
        staticmethod(lambda *_a, **_k: klines),
    )


def test_topup_appends_only_new_bars(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A top-up appends only bars newer than the cache's last ts."""
    round_dir, rids, _at = cohort_round
    cohort_def = {"experiment_dir": str(round_dir), "permutation_ids": list(rids)}
    conduit, arrow = tmp_path / "conduit", tmp_path / "arrow"

    head = frozen_klines.head(int(frozen_klines.height * 0.8))
    _serve_arrow(monkeypatch, head)
    prediction_cache.backfill(cohort_def, _SERIES, conduit, arrow)
    n0 = pl.read_ipc(prediction_cache.cache_path(_SERIES, conduit), memory_map=True).height

    _serve_arrow(monkeypatch, frozen_klines)  # the series now carries the full history
    appended = prediction_cache.top_up(cohort_def, _SERIES, conduit, arrow)
    n1 = pl.read_ipc(prediction_cache.cache_path(_SERIES, conduit), memory_map=True).height

    assert appended > 0
    assert n1 == n0 + appended


def test_topup_is_idempotent(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running a minute over the same closed bars appends nothing and leaves
    the cache byte-identical — a missed tick self-corrects, a repeated tick is a
    no-op."""
    round_dir, rids, _at = cohort_round
    cohort_def = {"experiment_dir": str(round_dir), "permutation_ids": list(rids)}
    conduit, arrow = tmp_path / "conduit", tmp_path / "arrow"
    _serve_arrow(monkeypatch, frozen_klines)

    prediction_cache.backfill(cohort_def, _SERIES, conduit, arrow)
    path = prediction_cache.cache_path(_SERIES, conduit)
    before = path.read_bytes()

    appended = prediction_cache.top_up(cohort_def, _SERIES, conduit, arrow)
    assert appended == 0
    assert path.read_bytes() == before
