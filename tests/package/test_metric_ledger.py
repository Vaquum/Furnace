"""Tests for slice S14: adapt experiment_runner UI to Limen's new
22-column decoder-level backtest metric ledger from `vaquum_limen` PR #520
(>= 3.2.1). Source-level checks against app.py — the constant
`_BACKTEST_LEDGER_COLUMNS` is the single source of truth and the three
downstream surfaces (results table, round-status stats, search index)
must all splat it in so they stay in sync.

Without this adapter every post-3.2.1 round renders the results table
as id + confusion only, the stats panel as confusion only, and the
search index as empty — the old `backtest_*` scalar columns the UI
was looking for were retired in Limen PR #520 and the new ledger
columns weren't yet wired."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / "experiment_runner"

# The 22 columns Limen PR #520 ships, in declaration order. The CSV
# carries each one prefixed with `backtest_`.
_LEDGER_BARE: tuple[str, ...] = (
    "edge_per_signal_bps_p5",
    "edge_per_signal_bps_p50",
    "edge_per_signal_bps_p95",
    "trade_pnl_net_bps_p5",
    "trade_pnl_net_bps_p50",
    "trade_pnl_net_bps_p95",
    "cost_drag_bps_p5",
    "cost_drag_bps_p50",
    "cost_drag_bps_p95",
    "rolling_return_net_bps_p5",
    "rolling_return_net_bps_p50",
    "rolling_return_net_bps_p95",
    "return_on_exposure_p5",
    "return_on_exposure_p50",
    "return_on_exposure_p95",
    "drawdown_depth_bps_p5",
    "drawdown_depth_bps_p50",
    "drawdown_depth_bps_p95",
    "drawdown_duration_days_p5",
    "drawdown_duration_days_p50",
    "drawdown_duration_days_p95",
    "cvar_95_return_bps",
)


def _assignment_value(node: ast.AST, name: str) -> ast.AST | None:
    """If `node` assigns the module-scope constant `name`, return its value
    expression. Handles both plain (`x = ...`) and annotated (`x: T = ...`)
    forms. Returns None for any other node."""
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == name:
                return node.value
        return None
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        if node.target.id == name and node.value is not None:
            return node.value
    return None


def _tuple_element_label(elt: ast.AST, name: str) -> str:
    """Convert a tuple-literal element to its asserting form: a string
    literal returns itself; a `*NAME` starred-name returns `*NAME`.
    Anything else raises so a test failure points at the bad element."""
    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
        return elt.value
    if isinstance(elt, ast.Starred) and isinstance(elt.value, ast.Name):
        return f"*{elt.value.id}"
    raise AssertionError(f"{name} has non-string element: {ast.dump(elt)}")


def _module_const(text: str, name: str) -> tuple:
    """Parse app.py and return the tuple literal assigned to `name`.
    Handles both plain assignments (`x = (...)`) and annotated ones
    (`x: tuple[str, ...] = (...)`). String elements become themselves;
    `*NAME` starred-name elements become the string `*NAME` so the test
    can assert on the splat without needing to compute its expansion.
    Raises AssertionError if missing or not a tuple literal."""
    tree = ast.parse(text)
    for node in ast.walk(tree):
        value = _assignment_value(node, name)
        if value is None:
            continue
        if not isinstance(value, ast.Tuple):
            raise AssertionError(f"{name} is not a tuple literal")
        return tuple(_tuple_element_label(elt, name) for elt in value.elts)
    raise AssertionError(f"{name} not defined at module scope in app.py")


def test_backtest_ledger_columns_match_limen_pr_520_exactly() -> None:
    """The canonical constant `_BACKTEST_LEDGER_COLUMNS` carries all 22
    columns Limen PR #520 ships, each prefixed `backtest_`, in declaration
    order. This list is what the round CSV will hold for any round run
    against Limen >= 3.2.1; adding / removing / reordering items here
    must be deliberate (it changes which columns the entire UI surfaces)."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    cols = _module_const(text, "_BACKTEST_LEDGER_COLUMNS")
    expected = tuple(f"backtest_{c}" for c in _LEDGER_BARE)
    assert cols == expected, (
        f"_BACKTEST_LEDGER_COLUMNS drifted from Limen PR #520 ledger.\n"
        f"expected ({len(expected)}): {expected}\n"
        f"actual   ({len(cols)}): {cols}"
    )


def test_results_table_includes_full_ledger_and_confusion_metrics() -> None:
    """The results table renders `id`, four confusion / precision columns,
    then the 22-column ledger splatted in via `*_BACKTEST_LEDGER_COLUMNS`.
    Splatting (rather than re-listing) is the wire that keeps the three
    surfaces in sync — a ledger edit at the top of the module flows
    through to the table without a parallel edit."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    cols = _module_const(text, "_RESULTS_COLUMNS")
    assert cols == (
        "id",
        "precision",
        "recall",
        "confusion_tp_mean_return_pct",
        "confusion_fp_mean_return_pct",
        "*_BACKTEST_LEDGER_COLUMNS",
    )


def test_round_status_stats_panel_uses_ledger() -> None:
    """The stats panel — `_compute_round_stats` and the experiment page,
    both via `_metric_percentiles` — computes p5 / p25 / p50 / p75 / p95 for
    the same metric set the results table renders. `_STATS_METRICS` splats
    `_BACKTEST_LEDGER_COLUMNS` so the panel mirrors the results table
    without a parallel edit."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    cols = _module_const(text, "_STATS_METRICS")
    assert cols == (
        "precision",
        "recall",
        "confusion_tp_mean_return_pct",
        "confusion_fp_mean_return_pct",
        "*_BACKTEST_LEDGER_COLUMNS",
    )


def test_search_metrics_mirror_results_table() -> None:
    """The search page exposes each metric as a range slider. Mirror the
    results table so the operator can filter on any metric they can see.
    `_SEARCH_METRICS` splats the ledger and lists the four confusion /
    precision metrics — same set as the results table minus `id`."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    cols = _module_const(text, "_SEARCH_METRICS")
    assert cols == (
        "precision",
        "recall",
        "confusion_tp_mean_return_pct",
        "confusion_fp_mean_return_pct",
        "*_BACKTEST_LEDGER_COLUMNS",
    )


def test_search_required_cols_demand_every_metric_plus_id() -> None:
    """A round is indexable only when its CSV carries every search
    metric. Any missing column means a pre-3.2.1 round (old backtest
    scalar surface) or a partial run — skip from the index until
    re-run. The required set is `id` plus every `_SEARCH_METRICS`
    entry, expressed in source as `{"id", *_SEARCH_METRICS}`."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    # _SEARCH_REQUIRED_COLS is a frozenset built from a set literal; assert
    # the source literal references `id` plus the splat of _SEARCH_METRICS.
    assert 'frozenset({"id", *_SEARCH_METRICS})' in text or \
           "frozenset({'id', *_SEARCH_METRICS})" in text


def test_ensure_round_index_invalidates_stale_schema_parquets() -> None:
    """The metric set changed in S14 (the `*_per_bar` derived columns from
    the prior search index are gone). Any `.index.parquet` written by the
    old code carries that old schema; the new `_compute_search_bounds`
    selecting `pl.col("precision")` would crash with `ColumnNotFoundError`
    on a stale cache hit. Validate the cached parquet's schema against
    the current `_SEARCH_REQUIRED_COLS` before trusting the mtime — if
    the schema doesn't carry every required metric, fall through and
    rebuild from the CSV."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    # The schema check must guard the cache-hit return so an mtime-fresh
    # but stale-schema parquet is rejected.
    assert "pl.read_parquet_schema(idx_path)" in text
    assert "_SEARCH_REQUIRED_COLS.issubset(pl.read_parquet_schema" in text


def test_ensure_round_index_excludes_no_signal_permutations() -> None:
    """The prior code's `.filter(pl.col("_bt") > 0)` did double duty:
    (a) guard per-bar divisions from div-by-zero; (b) exclude permutations
    whose backtest never ran (zero-signal / errored). The ledger refactor
    obsoletes (a) but (b) still applies — Limen >= 3.2.1 writes NaN ledger
    columns for permutations the backtest skipped, and NaN rows in
    `.index.parquet` would have to be coped with by the search UI's range
    sliders. Use `edge_per_signal_bps_p50` as the canary: a permutation
    with no tradable signals leaves every ledger percentile NaN."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    # The explicit non-null guard replaces the dropped `_bt > 0` filter.
    assert "filter(pl.col(\"backtest_edge_per_signal_bps_p50\").is_not_null())" in text


def test_ensure_round_index_no_longer_normalizes_by_bars_total() -> None:
    """The old indexer divided several metrics by `backtest_bars_total`
    to derive `*_per_bar` proxies for cross-round comparison. The new
    ledger ships per-signal / per-trade / per-day / fraction units so
    no normalization is needed — the indexer reads each metric column
    straight. Assert the old division-by-bars-total references are gone
    and the new straight-read shape is in place."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    # Old-shape references must be gone.
    for stale in (
        "backtest_bars_total",
        "backtest_bars_in_market_pct",
        "backtest_trades_count",
        "backtest_total_return_net_pct",
        "backtest_mean_kelly_pct",
        "backtest_sharpe_per_bar",
        "backtest_max_drawdown_pct",
        "trade_per_bar",
        "return_per_bar",
        "kelly_per_bar",
        "sharpe_per_bar",
    ):
        assert stale not in text, f"old metric reference {stale!r} still in app.py"
    # New-shape iteration over _SEARCH_METRICS must be present.
    assert "for m in _SEARCH_METRICS" in text


def test_pyproject_version_is_0_16_0() -> None:
    """Conventional Commits feat = minor bump. Version moves 0.15.0 → 0.16.0
    for the Limen YAML-manifest migration. This assertion is the repo's
    single pin on the current version — whichever slice bumps the version
    updates it here (and the CHANGELOG check below)."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == "0.16.0"


def test_changelog_has_v0_16_0_entry() -> None:
    """A CHANGELOG header for the new version must exist with at least
    one content line so the version-gate is satisfied."""
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    header_idx = next((i for i, ln in enumerate(lines) if ln.strip() == "# v0.16.0"), -1)
    assert header_idx != -1, "missing '# v0.16.0' header in CHANGELOG.md"
    next_header_idx = next(
        (i for i in range(header_idx + 1, len(lines)) if lines[i].startswith("# v")),
        len(lines),
    )
    content = [ln for ln in lines[header_idx + 1 : next_header_idx] if ln.strip()]
    assert content, "v0.16.0 CHANGELOG entry is empty"
