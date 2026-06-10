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


def _make_round(round_dir: Path, *, ids: list[str], edge: list[str], nan_metric: str | None = None) -> Path:
    """A minimal completed round: status.json + manifest.yml +
    limen_output/results.csv carrying ``id`` + every search metric. ``edge`` is
    written verbatim into ``backtest_edge_bps_p50`` (the index canary) so a test
    can force the all-``"nan"`` (str-inferred) column a never-ran backtest
    leaves behind. ``nan_metric``, when set, writes ``"nan"`` into that one
    column for every row — an indexed-but-NaN metric (e.g. cvar below 20 bars)."""
    round_dir.mkdir(parents=True)
    (round_dir / "status.json").write_text(json.dumps({"state": "completed"}))
    (round_dir / "manifest.yml").write_text("schema_version: '1.0'\n")
    lo = round_dir / "limen_output"
    lo.mkdir()
    cols = ["id", *_SEARCH_METRICS]
    lines = [",".join(cols)]
    for i, rid in enumerate(ids):
        row = {c: "0.5" for c in _SEARCH_METRICS}
        row["backtest_edge_bps_p50"] = edge[i]
        if nan_metric is not None:
            row[nan_metric] = "nan"
        row["id"] = rid
        lines.append(",".join(str(row[c]) for c in cols))
    (lo / "results.csv").write_text("\n".join(lines) + "\n")
    # round_data.jsonl mirrors Limen's output: one line per permutation, keyed
    # by `round_id` == the CSV `id` (the permutation_id) — the key the bundle's
    # jsonl filter selects on. Pinning it here is what proves that filter keeps
    # the right lines (and does not silently empty the jsonl on real data).
    rd = [
        json.dumps({"round_id": rid, "_round_index": i, "round_params": {}, "preds": [], "alignment": []})
        for i, rid in enumerate(ids)
    ]
    (lo / "round_data.jsonl").write_text("\n".join(rd) + "\n")
    return round_dir


def test_no_backtest_ledger_round_indexes_empty_without_crashing(tmp_path: Path) -> None:
    """An all-``"nan"`` (str-inferred) backtest column — a round whose every
    permutation lacks a backtest ledger — yields an empty index with the
    canonical schema, not an ``is_not_nan`` InvalidOperationError."""
    rd = _make_round(tmp_path / "exp" / "rounds" / "0001", ids=[_HEX_ID, _HEX_ID[::-1]], edge=["nan", "nan"])
    idx = _ensure_round_index(rd)
    assert idx is not None
    assert pl.read_parquet(idx).height == 0
    assert dict(pl.read_parquet_schema(idx)) == _INDEX_SCHEMA


def test_no_signal_flat_zero_permutation_is_indexed(tmp_path: Path) -> None:
    """The headline behavior change: under the all-bars ledger a no-signal
    permutation is a real all-zero row (``backtest_edge_bps_p50 == 0.0``), not
    NaN, so it is KEPT in the index — where the old per-signal ledger left it
    NaN and dropped it. A ``0.0`` canary survives; only the never-ran ``"nan"``
    row is dropped."""
    rd = _make_round(
        tmp_path / "exp" / "rounds" / "0003",
        ids=[_HEX_ID, _HEX_ID[::-1]],
        edge=["0.0", "nan"],
    )
    idx = _ensure_round_index(rd)
    assert idx is not None
    df = pl.read_parquet(idx)
    assert df.height == 1  # the flat 0.0 perm kept; the never-ran NaN perm dropped
    assert df.get_column("permutation_id")[0] == _HEX_ID


def test_search_bounds_coalesce_all_nan_metric_to_null(tmp_path: Path) -> None:
    """A metric uniformly NaN across the index — e.g. ``backtest_cvar_95_pnl_bps``
    when every indexed permutation has < 20 bars — must yield ``null`` bounds,
    not NaN. NaN is not valid JSON and would break ``/search/bounds.json``; the
    all-bars ledger makes this reachable because no-signal / short rows are now
    indexed rather than dropped."""
    from furnace.app import _compute_search_bounds

    _make_round(
        tmp_path / "experiments" / "exp" / "rounds" / "0001",
        ids=[_HEX_ID, _HEX_ID[::-1]],
        edge=["1.0", "2.0"],
        nan_metric="backtest_cvar_95_pnl_bps",
    )
    bounds = _compute_search_bounds(tmp_path, {}, None)
    assert bounds["backtest_cvar_95_pnl_bps"] == {"min": None, "max": None}
    assert bounds["backtest_edge_bps_p50"]["min"] is not None


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


def test_round_without_status_json_is_skipped(tmp_path: Path) -> None:
    """A round dir with no status.json is incomplete — `_ensure_round_index`
    returns None instead of letting `read_status` raise FileNotFoundError, which
    would 500 the whole /search page (it scans every round in one pass)."""
    rd = _make_round(tmp_path / "exp" / "rounds" / "0004", ids=["1"], edge=["2.0"])
    (rd / "status.json").unlink()
    assert _ensure_round_index(rd) is None


def test_selection_bundle_filters_int_id_round(tmp_path: Path) -> None:
    """The selection bundle re-reads the round CSV and filters it by the String
    `permutation_id`s from the index. For an int-id round the CSV `id` is Int64,
    so the filter must cast it to String first — an `is_in(List(String))` against
    an Int64 column raises, the same dtype mismatch the indexer fixes. The bundle
    carries the runnable manifest and is named for its one experiment + round(s)."""
    import io
    import zipfile

    from furnace.app import _build_selection_bundle, _selection_bundle_base

    root = tmp_path
    _make_round(root / "experiments" / "intexp" / "rounds" / "0001", ids=["7", "8"], edge=["3.0", "4.0"])
    data = _build_selection_bundle(root, "intexp", ["0001"], {})

    z = zipfile.ZipFile(io.BytesIO(data))
    base = _selection_bundle_base("intexp", ["0001"])
    assert base == "intexp__r0001__bundle"
    names = z.namelist()
    assert f"{base}/manifest.yml" in names, names
    csvs = [n for n in names if n.endswith(".csv")]
    assert csvs, "bundle should carry a CSV for the matched int-id round"
    rows = [ln for ln in z.read(csvs[0]).decode().splitlines() if ln.strip()]
    assert len(rows) == 3  # header + the two matched int-id permutations
    # With no filter both perms are kept, and the round_data.jsonl carries both
    # sensor lines — keyed by `round_id`, which equals the CSV `id` per perm.
    jsonls = [n for n in names if n.endswith("round_data.jsonl")]
    assert jsonls, "bundle should carry the round's round_data.jsonl"
    kept = [json.loads(ln) for ln in z.read(jsonls[0]).decode().splitlines() if ln.strip()]
    assert sorted(r["round_id"] for r in kept) == ["7", "8"]


def test_selection_bundle_filters_round_data_jsonl_by_perm_id(tmp_path: Path) -> None:
    """The bundle's round_data.jsonl is filtered to the matched perms by its
    `round_id` field — which Limen writes equal to the CSV `id` (the
    permutation_id). A filter that keeps one of two perms keeps exactly that
    perm's sensor line, proving both that the filter runs *and* that
    `round_id` == permutation_id is the right key: a wrong key would drop every
    line and this assertion would fail on an empty jsonl."""
    import io
    import zipfile

    from furnace.app import _build_selection_bundle, _parse_filter_params

    _make_round(tmp_path / "experiments" / "exp" / "rounds" / "0001", ids=["7", "8"], edge=["3.0", "4.0"])
    # edge >= 3.5 keeps only perm "8" (edge 4.0); perm "7" (3.0) is filtered out.
    filters = _parse_filter_params({"backtest_edge_bps_p50_min": "3.5"})
    data = _build_selection_bundle(tmp_path, "exp", ["0001"], filters)

    z = zipfile.ZipFile(io.BytesIO(data))
    jsonls = [n for n in z.namelist() if n.endswith("round_data.jsonl")]
    assert jsonls, "bundle should carry the round's round_data.jsonl"
    kept = [json.loads(ln) for ln in z.read(jsonls[0]).decode().splitlines() if ln.strip()]
    assert [r["round_id"] for r in kept] == ["8"]  # only the matched perm's sensor line


def test_selection_bundle_rejects_path_traversal(tmp_path: Path) -> None:
    """A crafted experiment / round id resolves through _safe_child_dir, so it is
    a 404 — never a path-traversing filesystem read or an unsanitized filename."""
    from fastapi import HTTPException

    from furnace.app import _build_selection_bundle

    _make_round(tmp_path / "experiments" / "exp" / "rounds" / "0001", ids=["1"], edge=["1.0"])
    for exp, rounds in [("../exp", ["0001"]), ("exp", ["../0001"]), ("exp", ["0001/x"])]:
        try:
            _build_selection_bundle(tmp_path, exp, rounds, {})
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError(f"expected 404 for experiment={exp!r} rounds={rounds!r}")


def test_selection_bundle_empty_filter_match_is_empty(tmp_path: Path) -> None:
    """A filter matching zero perms yields a header-only results.csv — never a
    silent fall-back to the full round."""
    import io
    import zipfile

    from furnace.app import _build_selection_bundle, _parse_filter_params

    _make_round(tmp_path / "experiments" / "exp" / "rounds" / "0001", ids=["7", "8"], edge=["3.0", "4.0"])
    filters = _parse_filter_params({"backtest_edge_bps_p50_min": "100"})
    data = _build_selection_bundle(tmp_path, "exp", ["0001"], filters)

    z = zipfile.ZipFile(io.BytesIO(data))
    csvs = [n for n in z.namelist() if n.endswith(".csv")]
    assert csvs
    rows = [ln for ln in z.read(csvs[0]).decode().splitlines() if ln.strip()]
    assert len(rows) == 1  # header only — zero perms matched the filter
    # The round_data.jsonl is filtered by the same perm set, so it is empty too —
    # never a silent fall-back to the full round's sensor lines.
    jsonls = [n for n in z.namelist() if n.endswith("round_data.jsonl")]
    assert jsonls
    assert [ln for ln in z.read(jsonls[0]).decode().splitlines() if ln.strip()] == []


def test_selection_bundle_rejects_multiple_rounds(tmp_path: Path) -> None:
    """A bundle is the file form of a cohort, and a cohort is one round: any
    multi-round selection is a 400 at the bundle chokepoint, whatever the
    rounds' manifests."""
    from fastapi import HTTPException

    from furnace.app import _build_selection_bundle

    _make_round(tmp_path / "experiments" / "exp" / "rounds" / "0001", ids=["1"], edge=["1.0"])
    _make_round(tmp_path / "experiments" / "exp" / "rounds" / "0002", ids=["2"], edge=["2.0"])
    try:
        _build_selection_bundle(tmp_path, "exp", ["0001", "0002"], {})
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "one round" in str(exc.detail)
    else:
        raise AssertionError("expected 400 for a multi-round selection")
