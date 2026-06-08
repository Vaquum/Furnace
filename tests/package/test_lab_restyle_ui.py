"""Tests for slice S15: restyle furnace to ~/dev/the-lab and
redesign the jobs and round pages. Source-level checks against the
templates, static assets, and app.py — the codebase's established test
idiom (app.py can't be imported without fastapi, so the tests assert on
source text rather than running the app).

The slice has two halves. The restyle half is value-only: the-lab's
near-black canvas, yellow accent, and IBM Plex fonts replace the old
light theme with the :root token NAMES unchanged. The redesign half
changes capability — the jobs page collapses experiments + rounds into
one expandable table carrying six trade-pnl / drawdown analytic columns,
a creation modal replaces the inline new-experiment form, the round
viewer's status block collapses to one bar with experiment / round
selectors, and search becomes reachable from the jobs page via an
embedded modal."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / "furnace"
TEMPLATES = PKG_DIR / "templates"
STATIC = PKG_DIR / "static"


def test_base_html_loads_ibm_plex_fonts() -> None:
    """The restyle adopts the-lab's typeface: IBM Plex Sans for prose and
    IBM Plex Mono for code / numerals, loaded from Google Fonts in the
    shared base template so every page inherits them."""
    html = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "fonts.googleapis.com" in html
    assert "IBM+Plex+Sans" in html
    assert "IBM+Plex+Mono" in html


def test_style_css_carries_the_lab_dark_palette() -> None:
    """The restyle is value-only — the :root token names are unchanged,
    but the canvas, accent, and font tokens now hold the-lab's exact
    dark-theme values so the two apps read as one tool."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "--paper: #121212;" in css      # the-lab --bg (near-black canvas)
    assert "--accent: #DDD941;" in css     # the-lab yellow accent
    assert '--font-sans: "IBM Plex Sans"' in css


def test_jobs_landing_is_one_expandable_table() -> None:
    """The jobs page collapses the old two-table layout (experiments,
    then a rounds table per experiment) into a single experiments-table
    whose round rows are children — hidden until their parent's chevron
    expands them. data-parent ties each round row to its experiment id."""
    jobs = (TEMPLATES / "jobs.html").read_text(encoding="utf-8")
    rows = (TEMPLATES / "_jobs_rows.html").read_text(encoding="utf-8")
    assert jobs.count('class="experiments-table"') == 1
    assert 'class="exp-row"' in rows
    assert 'class="round-row"' in rows
    assert 'data-parent="{{ exp.id }}"' in rows
    assert 'class="chevron" data-toggle=' in rows


def test_jobs_table_surfaces_trade_pnl_and_drawdown_columns() -> None:
    """Each row carries six analytic columns — trade-pnl and
    drawdown-depth, each at p5 / p50 / p95 — sourced from the round
    CSV's backtest ledger percentile columns."""
    app = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    for key, col in (
        ("tp_p5", "backtest_pnl_bps_p5"),
        ("tp_p50", "backtest_pnl_bps_p50"),
        ("tp_p95", "backtest_pnl_bps_p95"),
        ("dd_p5", "backtest_drawdown_bps_p5"),
        ("dd_p50", "backtest_drawdown_bps_p50"),
        ("dd_p95", "backtest_drawdown_bps_p95"),
    ):
        assert f'"{key}": "{col}"' in app, f"_ANALYTIC_COLUMNS missing {key}"
    jobs = (TEMPLATES / "jobs.html").read_text(encoding="utf-8")
    assert "Trade PnL p5" in jobs
    assert "Drawdown p95" in jobs


def test_round_analytic_is_cross_permutation_median_then_round_mean() -> None:
    """A round's analytic value is the cross-permutation median of the
    ledger column — the same p50 the round viewer reports. An
    experiment's value is the mean of that across the rounds carrying
    it, so a one-round experiment reads identically to its round."""
    app = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    assert "def _round_analytics(" in app
    assert "def _experiment_analytics(" in app
    assert "s.quantile(0.50)" in app            # round = cross-permutation median
    assert "sum(vals) / len(vals)" in app       # experiment = mean across rounds


def test_round_analytics_resolves_canonical_results_csv() -> None:
    """_round_analytics resolves the round's results CSV through the shared
    _round_csv_path helper — the fixed limen_output/results.csv path that
    every CSV call site in app.py uses, limen's own authoritative file —
    not a glob, whose filesystem-order dependence could read a stray CSV."""
    app = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    body = app[app.index("def _round_analytics(") :][:900]
    assert 'glob("*.csv")' not in body
    assert "_round_csv_path(round_dir)" in body


def test_metric_fmt_jinja_filter_registered() -> None:
    """The analytic cells render through a metric_fmt Jinja filter backed
    by _format_metric — magnitude-adaptive precision, em dash for a
    missing (pre-ledger) value."""
    app = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    assert "def _format_metric(" in app
    assert 'templates.env.filters["metric_fmt"] = _format_metric' in app


def test_create_modal_is_global_in_base() -> None:
    """The creation modal is part of the shared layout, not the jobs page.
    The `+` control-bar button, the `#create-modal` it opens, and the
    create_modal.js that wires them all live in base.html, so creating an
    experiment or round works from every view."""
    assert (STATIC / "create_modal.js").is_file()
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert 'id="create-modal"' in base
    assert 'id="open-create"' in base
    assert "create_modal.js" in base
    # No longer carried by the jobs page itself.
    assert 'id="create-modal"' not in (TEMPLATES / "jobs.html").read_text(encoding="utf-8")


def test_create_experiment_endpoint_accepts_json() -> None:
    """The modal creates an experiment over fetch(), so POST /experiments
    accepts a JSON body and answers with the new id as JSON. The old
    form-encoded + 303-redirect path stays for the no-JS case."""
    app = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    assert 'startswith("application/json")' in app
    assert 'JSONResponse({"id": eid})' in app
    # A JSON name must be a real string — null / numbers are a 400, not
    # coerced into a surprising experiment id.
    assert '"name must be a string"' in app


def test_working_json_endpoint_exists() -> None:
    """The modal loads an existing experiment's working state — code,
    args, version — over a GET working.json endpoint to prefill its
    round-args controls."""
    app = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    assert '"/experiments/{experiment_id}/working.json"' in app
    assert "def working_json(" in app


def test_round_viewer_has_experiment_and_round_selectors() -> None:
    """The round viewer's controls are two dropdowns — experiment and
    round — placed in the top control bar's {% block controls %}, so the
    operator can move anywhere without returning to the jobs page;
    round_page passes the sibling rounds and all experiments for them."""
    html = (TEMPLATES / "round.html").read_text(encoding="utf-8")
    assert "{% block controls %}" in html
    assert 'class="round-select" aria-label="Experiment"' in html
    assert 'class="round-select" aria-label="Round"' in html
    app = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    assert '"rounds": _experiment_rounds(' in app
    assert '"experiments": _all_experiments(root)' in app


def test_search_is_a_global_link_in_base() -> None:
    """The search icon is a global link to the full /search page (the old
    nav-less modal/iframe is gone), part of the shared layout so search is
    reachable from every view, not just the jobs page."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert '<a href="/search" id="open-search"' in base
    assert 'id="search-modal"' not in base
    assert "/search?embed=1" not in base
    assert 'id="open-search"' not in (TEMPLATES / "jobs.html").read_text(encoding="utf-8")


def test_search_index_build_uses_unique_temp() -> None:
    """The search index builder writes each round's .index.parquet through a
    uniquely-named temp file. /search/bounds.json and /search/rounds.json are
    fetched concurrently and both rebuild the same round's index — a shared
    temp filename raced their os.replace calls into a FileNotFoundError 500."""
    app = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    body = app[app.index("def _ensure_round_index(") :]
    body = body[: body.index("\ndef ", 1)]
    assert "secrets.token_hex" in body
    assert "os.replace(tmp, idx_path)" in body


def test_search_page_drops_stale_jobs_link() -> None:
    """The search page no longer carries its own `← jobs` back-link — the
    persistent JOBS nav in the shared control bar already does that. Its
    filter labels use the short display-label form the rest of the UI uses."""
    html = (TEMPLATES / "search.html").read_text(encoding="utf-8")
    assert "topbar-link" not in html
    assert "← jobs" not in html
    assert "metric | display_label" in html


def test_embed_query_param_drops_nav_chrome() -> None:
    """A page rendered inside an in-app modal iframe must not show the
    top control bar — base.html gates the whole <header class="topbar">
    on the absence of the embed query param. One gate in the shared
    layout covers every embedded view (the jobs-page search modal)."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "request.query_params.get('embed')" in base
    assert '<header class="topbar">' in base
    assert base.index("request.query_params.get('embed')") < base.index("<header")


def test_views_share_one_top_control_bar() -> None:
    """the-lab's structural pattern — the main area splits into a top
    control bar and the content below it. base.html provides one shared
    <header class="topbar"> that opens with the persistent JOBS nav, then
    each view's {% block controls %} and the {% block meta %} readout;
    every page-level view extends base.html, so the bar is present and
    consistently placed across the app while its contents change per view."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert '<header class="topbar">' in base
    assert "{% block controls %}" in base
    assert '<main class="content">' in base
    # The persistent local-nav home link, present on every view.
    assert 'class="topbar-nav" href="/"' in base
    for view in ("jobs", "experiment", "round", "search", "results"):
        html = (TEMPLATES / f"{view}.html").read_text(encoding="utf-8")
        assert '{% extends "base.html" %}' in html, f"{view}.html does not extend base"
