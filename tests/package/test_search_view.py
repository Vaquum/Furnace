"""Tests for the redesigned search view (slice: full-page search + two-row
filter rail + hierarchical results table).

Source-level assertions against the templates — the search view is rendered
client-side, so these pin the structural contract the redesign delivers:

  * the search icon opens the full page, not the old modal/iframe;
  * filtering is live (no Search button);
  * each metric filter is a two-row block (label ↔ range, then min ↔ slider ↔
    max);
  * the results pane reuses the landing `experiments-table` as an
    experiment → rounds hierarchy with a match-count column.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = REPO_ROOT / "furnace" / "templates"


def _search() -> str:
    return (TEMPLATES / "search.html").read_text(encoding="utf-8")


def _base() -> str:
    return (TEMPLATES / "base.html").read_text(encoding="utf-8")


def test_search_icon_opens_full_page_not_modal() -> None:
    """The persistent search icon navigates to the full /search page; the old
    nav-less modal + iframe are gone."""
    base = _base()
    assert '<a href="/search" id="open-search"' in base
    assert 'id="search-modal"' not in base
    assert "search-frame" not in base


def test_no_redundant_search_button() -> None:
    """Filtering is live on every slider move — there is no Search button."""
    assert 'id="search-btn"' not in _search()


def test_filters_are_two_row_blocks() -> None:
    """Each metric filter is a two-row block: row 1 label ↔ current range,
    row 2 min ↔ slider ↔ max (the bounds flank the slider on its own row)."""
    text = _search()
    assert 'class="filter-block" data-metric=' in text
    assert 'class="filter-top"' in text and 'class="filter-bottom"' in text
    assert 'class="bound bound-min"' in text and 'class="bound bound-max"' in text


def test_results_table_is_experiment_rounds_hierarchy_with_matches() -> None:
    """The results pane reuses the landing `experiments-table` as an
    experiment → rounds hierarchy (chevron + round rows) with a Matches count
    and no latest-activity / last-status columns."""
    text = _search()
    assert "experiments-table search-table" in text
    assert "col-matches" in text
    # the hierarchy + chevron are built client-side from the flat rounds list
    assert "exp-row" in text and "round-row" in text and "chevron" in text
    # dropped columns from the landing table
    assert "Latest activity" not in text and "Last status" not in text
