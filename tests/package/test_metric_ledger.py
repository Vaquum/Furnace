"""Tests for the bar-based backtest-ledger adapter: Furnace's UI follows
Limen's 20-column bar-based metric ledger (`vaquum_limen` >= 3.20.0,
Vaquum/Limen#582). Source-level checks against app.py — the constant
`_BACKTEST_LEDGER_COLUMNS` is the single source of truth and the three
downstream surfaces (results table, round-status stats, search index)
must all splat it in so they stay in sync.

Without this adapter every post-3.20.0 round renders the results table
as id + confusion only, the stats panel as confusion only, and the
search index as empty — the old `backtest_*` ledger columns the UI
was looking for were renamed / dropped in Limen #582 and the new ledger
columns weren't yet wired."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / "furnace"

# The 20 columns Limen #582 ships, in declaration order. The CSV
# carries each one prefixed with `backtest_`.
_LEDGER_BARE: tuple[str, ...] = (
    "edge_bps_p5",
    "edge_bps_p50",
    "edge_bps_p95",
    "pnl_bps_p5",
    "pnl_bps_p50",
    "pnl_bps_p95",
    "cost_bps_p5",
    "cost_bps_p50",
    "cost_bps_p95",
    "drawdown_bps_p5",
    "drawdown_bps_p50",
    "drawdown_bps_p95",
    "wins_per_bar",
    "pnl_per_bar_bps",
    "avg_win_bps",
    "avg_loss_bps",
    "cvar_95_pnl_bps",
    "trades_per_bar",
    "inventory_per_bar",
    "cost_per_bar_bps",
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


def test_backtest_ledger_columns_match_limen_582_exactly() -> None:
    """The canonical constant `_BACKTEST_LEDGER_COLUMNS` carries all 20
    columns Limen #582 ships, each prefixed `backtest_`, in declaration
    order. This list is what the round CSV will hold for any round run
    against Limen >= 3.20.0; adding / removing / reordering items here
    must be deliberate (it changes which columns the entire UI surfaces)."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    cols = _module_const(text, "_BACKTEST_LEDGER_COLUMNS")
    expected = tuple(f"backtest_{c}" for c in _LEDGER_BARE)
    assert cols == expected, (
        f"_BACKTEST_LEDGER_COLUMNS drifted from Limen #582 ledger.\n"
        f"expected ({len(expected)}): {expected}\n"
        f"actual   ({len(cols)}): {cols}"
    )


def test_results_table_includes_full_ledger_and_confusion_metrics() -> None:
    """The results table renders `id`, four confusion / precision columns,
    then the 20-column ledger splatted in via `*_BACKTEST_LEDGER_COLUMNS`.
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
    metric. Any missing column means a pre-3.20.0 round (old backtest
    ledger surface) or a partial run — skip from the index until
    re-run. The required set is `id` plus every `_SEARCH_METRICS`
    entry, expressed in source as `{"id", *_SEARCH_METRICS}`."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    # _SEARCH_REQUIRED_COLS is a frozenset built from a set literal; assert
    # the source literal references `id` plus the splat of _SEARCH_METRICS.
    assert 'frozenset({"id", *_SEARCH_METRICS})' in text or \
           "frozenset({'id', *_SEARCH_METRICS})" in text


def test_ensure_round_index_invalidates_stale_schema_parquets() -> None:
    """A cached `.index.parquet` is trusted only when its schema is *exactly*
    the current `_INDEX_SCHEMA`. Exact match (not a subset) rebuilds on any
    column-set or dtype drift — a retired metric, or the old Int64
    `permutation_id` — so a stale parquet never feeds the cross-round
    `scan_parquet`. The prior subset check tested `_SEARCH_REQUIRED_COLS`,
    which carries `id`, against an index that renames `id` to `permutation_id`,
    so it never held and every search rebuilt every round."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    # The cache-hit return is guarded by an exact schema-equality check.
    assert "pl.read_parquet_schema(idx_path)) == _INDEX_SCHEMA" in text
    # _INDEX_SCHEMA pins permutation_id as String (Limen round_ids are hex).
    assert '"permutation_id": pl.String()' in text


def test_ensure_round_index_excludes_permutations_without_a_backtest_ledger() -> None:
    """A permutation whose backtest never ran (Limen writes NaN across the
    whole ledger) is excluded by an `is_not_null & is_not_nan` guard on the
    `backtest_edge_bps_p50` canary. Under the bar-based ledger a no-signal
    permutation is a real all-zero row (p50 0.0, not NaN), so it is kept — the
    guard drops only the never-ran rows. The canary is cast to Float64 *first*:
    a round where every permutation lacks a ledger writes the whole column as
    the text "nan", which `read_csv` infers as a str column, and `is_not_nan`
    is undefined on str — the cast is what stops that 500ing /search."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    # Canary cast to Float64 before the null/NaN guard (dtype-robust).
    assert 'pl.col("backtest_edge_bps_p50").cast(pl.Float64, strict=False)' in text
    assert "edge.is_not_null() & edge.is_not_nan()" in text


def test_ensure_round_index_no_longer_normalizes_by_bars_total() -> None:
    """The old indexer divided several metrics by `backtest_bars_total`
    to derive `*_per_bar` proxies for cross-round comparison. The
    bar-based ledger ships per-bar / fraction units so no normalization
    is needed — the indexer reads each metric column straight. Assert the
    old division-by-bars-total references are gone and the new
    straight-read shape is in place."""
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


def test_pyproject_version_is_0_31_1() -> None:
    """S77 (warm-up-window cohort serving) bumps the version to 0.31.1.
    This assertion is the repo's single pin on the current version — whichever
    slice bumps the version updates it here."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == "0.31.1"


def test_changelog_has_v0_31_1_entry() -> None:
    """A CHANGELOG header for the new version must exist with at least
    one content line so the version-gate is satisfied."""
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    header_idx = next((i for i, ln in enumerate(lines) if ln.strip() == "# v0.31.1"), -1)
    assert header_idx != -1, "missing '# v0.31.1' header in CHANGELOG.md"
    next_header_idx = next(
        (i for i in range(header_idx + 1, len(lines)) if lines[i].startswith("# v")),
        len(lines),
    )
    content = [ln for ln in lines[header_idx + 1 : next_header_idx] if ln.strip()]
    assert content, "v0.31.1 CHANGELOG entry is empty"
