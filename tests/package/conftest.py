"""Shared cohort-inference test fixtures.

A committed real BTC 1h kline slice (``fixtures/cohort_klines_1h.parquet``,
pulled once from Limen's Hugging Face spot dataset) stands in for the network.
``get_spot_klines`` is patched to serve that slice, honouring the requested
date window, so the suite trains and predicts on **real** data — offline,
deterministic, no synthetic values.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import polars as pl
import pytest
from limen.cli.commands.run import run_experiment
from limen.data import historical_data

_FIXTURE = Path(__file__).parent / "fixtures" / "cohort_klines_1h.parquet"

# A bar after test_end (2025-02-15) — out-of-sample, so the cohort returns a
# real prediction rather than reason='inside-training-window'.
_AT = "2025-02-18 12:00:00"

_YAML = dedent("""\
    schema_version: "1.0"
    metadata:
      name: furnace_cohort_test
      mode: development
    sfd:
      manifest:
        type: ml
        data_source:
          method: limen.data.HistoricalData.get_spot_klines
          params:
            kline_size: 3600
        split_dates:
          train_start: "2025-01-01"
          train_end: "2025-02-01"
          val_start: "2025-02-01"
          val_end: "2025-02-08"
          test_start: "2025-02-08"
          test_end: "2025-02-15"
        indicators:
          - func: limen.indicators.roc
            params:
              period: 1
              group: momentum
          - func: limen.indicators.wilder_rsi
            params:
              period: 14
              group: momentum
        target:
          name: quantile_flag
          class: limen.targets.QuantileBinaryTarget
          fit_params:
            source_column: roc_1
            quantile: 0.5
          transform_params:
            shift: -1
        scaler:
          from_params: scaler_type
        reference_architecture: limen.sfd.reference_architecture.logreg_binary
      params:
        scaler_type: [logreg]
        C: [1.0, 0.5]
        random_state: [42]
    uel:
      n_permutations: 2
      search_strategy:
        type: random
      output_format: csv
""")


def _parse_limit(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace(" ", "T")).replace(tzinfo=UTC)


def _fake_spot_klines(
    *_args: object,
    start_date_limit: str | None = None,
    end_date_limit: str | None = None,
    **_kwargs: object,
) -> pl.DataFrame:
    """Drop-in for ``HistoricalData.get_spot_klines`` that serves the committed
    real slice, filtered to the requested ``[start, end]`` window. Never
    touches the network."""
    df = pl.read_parquet(_FIXTURE)
    if start_date_limit is not None:
        df = df.filter(pl.col("datetime") >= _parse_limit(start_date_limit))
    if end_date_limit is not None:
        df = df.filter(pl.col("datetime") <= _parse_limit(end_date_limit))
    return df


@pytest.fixture
def frozen_klines() -> pl.DataFrame:
    """The committed real kline slice, read straight from the fixture."""
    return pl.read_parquet(_FIXTURE)


@pytest.fixture
def fake_spot_klines():
    """Patch get_spot_klines to serve the frozen real slice for one test, so a
    call through Trainer / inference stays offline and deterministic."""
    with patch.object(
        historical_data.HistoricalData,
        "get_spot_klines",
        staticmethod(_fake_spot_klines),
    ):
        yield


@pytest.fixture(scope="session")
def cohort_round(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[str], str]:
    """A small real round trained on the frozen klines, built once per session
    (offline). Returns ``(round_dir, permutation_ids, at)``."""
    exp = tmp_path_factory.mktemp("cohort") / "round"
    exp.mkdir()
    with (
        patch.object(
            historical_data.HistoricalData,
            "get_spot_klines",
            staticmethod(_fake_spot_klines),
        ),
        patch("click.echo"),
        patch("click.secho"),
    ):
        yaml_path = exp.parent / "exp.yaml"
        yaml_path.write_text(
            _YAML.replace("output_format: csv", f'output_format: csv\n  output_path: "{exp}"')
        )
        run_experiment(yaml_path)
    rids = [
        json.loads(line)["round_id"]
        for line in (exp / "round_data.jsonl").read_text().splitlines()
        if line.strip()
    ]
    return exp, rids, _AT
