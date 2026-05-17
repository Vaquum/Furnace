"""Tests for slice S11: surface test-period dates and authoritative
kline-size on the round status section. Source-level checks against
run_round.py (where period.json is written), app.py (where it's loaded
and the Jinja filter is registered), and round.html (where the new
status fields render)."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / 'experiment_runner'


def test_run_round_writes_period_json() -> None:
    """After uel.run() returns, run_round.py persists per-round metadata to
    <round_dir>/period.json — test-period bounds from uel._alignment plus
    the authoritative kline_size derived from data row spacing."""
    text = (PKG_DIR / 'run_round.py').read_text(encoding='utf-8')
    assert 'period.json' in text
    # The alignment list is Limen's per-permutation source of truth for
    # test-period dates. The runner reads from it, not from its own slice.
    assert '_alignment' in text
    # Authoritative kline_size: derived from the actual datetime diffs of
    # the fetched data, not echoed from args.json.
    assert 'total_seconds()' in text
    # _collect_period_info is the helper; it returns the dict we persist.
    assert '_collect_period_info' in text


def test_app_loads_period_json_into_round_context() -> None:
    """round_page reads <round_dir>/period.json (when present) and passes
    it into the round.html context as `period`."""
    text = (PKG_DIR / 'app.py').read_text(encoding='utf-8')
    assert 'period.json' in text
    # The context dict includes the loaded period under the `period` key.
    assert '"period": period' in text
    # Jinja filter is registered so the template can pretty-print
    # kline_size_seconds → '4h' / '15m' / etc.
    assert 'kline_size_display' in text


def test_kline_size_display_filter_logic() -> None:
    """The _format_kline_size helper turns seconds into the largest
    whole-unit string the interval divides into. Source-level shape
    check (app.py can't be imported in dev-extras — no fastapi)."""
    text = (PKG_DIR / 'app.py').read_text(encoding='utf-8')
    assert 'def _format_kline_size' in text
    helper_idx = text.index('def _format_kline_size')
    body = text[helper_idx:helper_idx + 1200]
    # Covers day / hour / minute, falls through to raw seconds otherwise.
    assert "(86400, 'd')" in body
    assert "(3600, 'h')" in body
    assert "(60, 'm')" in body


def test_round_page_sets_no_store_cache_control() -> None:
    """The round page's state changes while a user has it open (running
    → completed, diff appears, period.json gets written). Without
    Cache-Control: no-store the browser serves the older HTML and the
    user has to hard-refresh to see the new fields populate."""
    text = (PKG_DIR / 'app.py').read_text(encoding='utf-8')
    # The round_page route assigns its response to a name so it can set
    # headers (rather than returning TemplateResponse(...) inline).
    assert 'response = templates.TemplateResponse(' in text
    assert 'response.headers["Cache-Control"] = "no-store"' in text


def test_round_html_shows_test_period_and_kline_size() -> None:
    """round.html surfaces the new fields in the status grid and drops
    the old wall-clock Started/Ended labels (they were misleading — the
    process run time has nothing to do with the data window the results
    represent)."""
    html = (PKG_DIR / 'templates' / 'round.html').read_text(encoding='utf-8')
    assert 'Test period start' in html
    assert 'Test period end' in html
    assert 'Kline size' in html
    assert 'kline_size_display' in html
    # The old wall-clock dt labels are gone from the visible status grid.
    assert '<dt>Started</dt>' not in html
    assert '<dt>Ended</dt>' not in html
