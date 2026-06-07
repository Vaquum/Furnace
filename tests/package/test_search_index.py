"""Functional regression test for ``furnace.app._ensure_round_index`` — the
search-index builder.

Two real Limen output shapes used to crash the builder, and because
``/search`` rebuilds every round's index in one pass, the first bad round 500ed
the whole page:

  * a round with no tradable signals writes every backtest-ledger percentile as
    the text ``"nan"``, which ``pl.read_csv`` infers as ``str`` — and the
    ``is_not_nan`` no-signal filter is only defined on a numeric dtype;
  * a round whose ``id`` is a SHA-256 hex string (Limen's round_id) — the old
    ``cast(pl.Int64)`` could not parse it.

These tests build a minimal completed round on disk and drive the real builder,
so a future change that reintroduces either crash fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from furnace.app import _INDEX_SCHEMA, _SEARCH_METRICS, _ensure_round_index

_HEX_ID = "a7c6c19b4e9f80eedfc2da3020dcf3d39433eed86c96ac0143099356b3e81547"


def _make_round(round_dir: Path, *, ids: list[str], edge: list[str]) -> Path:
    """A minimal completed round: status.json + manifest.yml +
    limen_output/results.csv carrying ``id`` + every search metric. ``edge`` is
    written verbatim into ``backtest_edge_per_signal_bps_p50`` (the no-signal
    canary) so a test can force the all-``"nan"`` (str-inferred) column."""
    round_dir.mkdir(parents=True)
    (round_dir / "status.json").write_text(json.dumps({"state": "completed"}))
    (round_dir / "manifest.yml").write_text("schema_version: '1.0'\n")
    lo = round_dir / "limen_output"
    lo.mkdir()
    cols = ["id", *_SEARCH_METRICS]
    lines = [",".join(cols)]
    for i, rid in enumerate(ids):
        row = {c: "0.5" for c in _SEARCH_METRICS}
        row["backtest_edge_per_signal_bps_p50"] = edge[i]
        row["id"] = rid
        lines.append(",".join(str(row[c]) for c in cols))
    (lo / "results.csv").write_text("\n".join(lines) + "\n")
    return round_dir


def test_no_signal_round_indexes_empty_without_crashing(tmp_path: Path) -> None:
    """An all-``"nan"`` (str-inferred) backtest column yields an empty index
    with the canonical schema — not an ``is_not_nan`` InvalidOperationError."""
    rd = _make_round(tmp_path / "exp" / "rounds" / "0001", ids=[_HEX_ID, _HEX_ID[::-1]], edge=["nan", "nan"])
    idx = _ensure_round_index(rd)
    assert idx is not None
    assert pl.read_parquet(idx).height == 0
    assert dict(pl.read_parquet_schema(idx)) == _INDEX_SCHEMA


def test_hex_id_round_indexes_with_string_permutation_id(tmp_path: Path) -> None:
    """A SHA-256 hex ``id`` is carried through as a String ``permutation_id`` —
    the Int64 cast used to crash here."""
    rd = _make_round(tmp_path / "exp" / "rounds" / "0002", ids=[_HEX_ID], edge=["12.5"])
    idx = _ensure_round_index(rd)
    assert idx is not None
    df = pl.read_parquet(idx)
    assert df.height == 1
    assert df.schema["permutation_id"] == pl.String
    assert df.get_column("permutation_id")[0] == _HEX_ID


def test_stale_int64_index_is_rebuilt(tmp_path: Path) -> None:
    """A cached ``.index.parquet`` written with the old Int64 ``permutation_id``
    fails the exact-schema check and is rebuilt as String, so the cross-round
    ``scan_parquet`` never sees a dtype-mismatched parquet."""
    rd = _make_round(tmp_path / "exp" / "rounds" / "0003", ids=["7"], edge=["3.0"])
    idx_path = rd / ".index.parquet"
    # Plant a stale index with the wrong permutation_id dtype, freshly mtimed.
    stale = pl.DataFrame(
        {"permutation_id": [7], "experiment_id": ["exp"], "round_id": ["0003"], "code_sha256": ["x"]}
    ).with_columns(pl.col("permutation_id").cast(pl.Int64))
    stale = stale.with_columns([pl.lit(0.0).alias(m) for m in _SEARCH_METRICS])
    stale.write_parquet(idx_path)
    assert dict(pl.read_parquet_schema(idx_path)) != _INDEX_SCHEMA  # precondition

    idx = _ensure_round_index(rd)
    assert idx is not None
    assert dict(pl.read_parquet_schema(idx)) == _INDEX_SCHEMA
    assert pl.read_parquet(idx).schema["permutation_id"] == pl.String


def test_search_bundle_filters_int_id_round(tmp_path: Path) -> None:
    """The `/search` download bundle re-reads each round CSV and filters it by
    the String `permutation_id`s from the index. For an int-id round the CSV
    `id` is Int64, so the filter must cast it to String first — an
    `is_in(List(String))` against an Int64 column raises, the same dtype
    mismatch the indexer fixes. Regression test for that sibling consumer."""
    import io
    import zipfile

    from furnace.app import _build_search_bundle

    root = tmp_path
    _make_round(root / "experiments" / "intexp" / "rounds" / "0001", ids=["7", "8"], edge=["3.0", "4.0"])
    data = _build_search_bundle(root, {}, ["intexp"])

    z = zipfile.ZipFile(io.BytesIO(data))
    csvs = [n for n in z.namelist() if n.endswith(".csv")]
    assert csvs, "bundle should carry a CSV for the matched int-id round"
    rows = [ln for ln in z.read(csvs[0]).decode().splitlines() if ln.strip()]
    assert len(rows) == 3  # header + the two matched int-id permutations
