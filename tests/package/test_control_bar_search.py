"""Tests for the control-bar / search-page slice: a build-commit control
bar, the search page, working manifest copy buttons, and flash-free
page loads.

Source-level checks against the templates, static assets, and style.css —
the codebase's test idiom (app.py / the runtime cannot be imported in the
package dev-extras, so the tests assert on source text).

Four capabilities:
  A. The top control bar shows the commit the build runs and links it to
     GitHub; its icons form one tight group, consistent on every view.
  B. The search page is a narrow filter rail beside a wide results table,
     with the slider knobs centred on the track at rest.
  C. Every manifest surface has a copy button that works over plain HTTP.
  D. A page load never flashes white."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / "furnace"
TEMPLATES = PKG_DIR / "templates"
STATIC = PKG_DIR / "static"


# ---- A. Control bar -------------------------------------------------------


def test_control_bar_links_the_build_commit_to_github() -> None:
    """The bar's rightmost element is the build identity — the package
    version and the commit SHA the running app is built from. The SHA is
    an <a> to that commit on GitHub (repo_url + /commit/ + the full SHA)."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert 'class="topbar-build"' in base
    assert "{{ repo_url }}/commit/{{ git_full_sha }}" in base
    assert "{{ git_short_sha }}" in base


def test_control_bar_right_side_order_is_icons_meta_build() -> None:
    """topbar-right is, left to right: the icon group, then the page meta
    block, then the build identity — the build commit always rightmost."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    right = base[base.index('class="topbar-right"') : base.index("</header>")]
    assert right.index('class="bar-icons"') < right.index("{% block meta %}")
    assert right.index("{% block meta %}") < right.index('class="topbar-build"')


def test_bar_icons_group_holds_page_local_then_global_icons() -> None:
    """The icon group carries the view's page-local icons (the bar_icons
    block) then the always-present global search + create. The two global
    icons are the rightmost of the cluster — page icons prepend, so search
    and + never move."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    icons = base[base.index('class="bar-icons"') :]
    icons = icons[: icons.index("</div>")]
    assert "{% block bar_icons %}" in icons
    assert icons.index("{% block bar_icons %}") < icons.index('id="open-search"')
    assert icons.index('id="open-search"') < icons.index('id="open-create"')


def test_round_page_icons_moved_to_the_shared_icon_group() -> None:
    """The round page's results / bundle / terminate icons render in the
    shared bar_icons block — appended to the global icons — not inside the
    meta readout."""
    html = (TEMPLATES / "round.html").read_text(encoding="utf-8")
    assert "{% block bar_icons %}" in html
    bar = html[html.index("{% block bar_icons %}") : html.index("{% block meta %}")]
    assert 'id="terminate-btn"' in bar
    assert "/results" in bar and "/bundle" in bar


def test_bar_icons_css_is_a_tight_group_and_honours_hidden() -> None:
    """`.bar-icons` is a tight inline cluster (the-lab's icon-group
    rhythm). `.bar-icon[hidden]` re-asserts the hidden attribute over the
    author display rule, so a hidden icon — a terminated round's terminate
    button — does not show. `.topbar-build` carries the build stamp."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".bar-icons {" in css
    assert ".bar-icon[hidden] { display: none; }" in css
    assert ".topbar-build {" in css


# ---- D. No flash on load --------------------------------------------------


def test_base_paints_the_dark_canvas_before_the_stylesheet() -> None:
    """An inline <style> in <head> paints the dark canvas and sets
    color-scheme before style.css loads, so a page load never flashes
    white. The inline style must come before the style.css <link>."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "color-scheme:dark" in base or "color-scheme: dark" in base
    assert "#121212" in base
    assert base.index("<style>") < base.index("/static/style.css")


# ---- B. Search page -------------------------------------------------------


def test_search_page_is_filter_rail_plus_table() -> None:
    """The search page is a narrow filter rail beside a wide results table — a
    .search-filters aside and a .search-results section inside .search-layout,
    not two equal stacked panels."""
    html = (TEMPLATES / "search.html").read_text(encoding="utf-8")
    assert 'class="search-layout"' in html
    assert 'class="search-filters"' in html
    assert 'class="search-results"' in html
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    body = css[css.index(".search-layout {") :][:160]
    assert "display: flex;" in body


def test_filter_rail_is_narrow_and_table_takes_the_rest() -> None:
    """The filter rail is a fixed narrow column; the results pane flexes to fill
    the rest of the width — the table gets the room."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    rail = css[css.index(".search-filters {") :]
    rail = rail[: rail.index("}")]
    assert "flex: 0 0" in rail
    results = css[css.index(".search-results {") :]
    results = results[: results.index("}")]
    assert "flex: 1 1 auto;" in results


def test_slider_thumb_is_centred_on_the_track_at_rest() -> None:
    """The 16px range thumb gets margin-top: 4px so it is centred on the
    24px runnable track at rest — without it the thumb sat 4px high until
    the 24px active size happened to fill the track."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    thumb = css[css.index("::-webkit-slider-thumb {") :]
    thumb = thumb[: thumb.index("}")]
    assert "margin-top: 4px;" in thumb


# ---- C. Manifest copy -----------------------------------------------------


def test_clipboard_helper_falls_back_for_non_secure_contexts() -> None:
    """clipboard.js exposes window.copyText — navigator.clipboard.writeText
    in a secure context, falling back to a hidden-textarea
    execCommand('copy') so copy works over plain HTTP (the deploy is not a
    secure context)."""
    js = (STATIC / "clipboard.js").read_text(encoding="utf-8")
    assert "window.copyText" in js
    assert "navigator.clipboard.writeText" in js
    assert "execCommand('copy')" in js


def test_base_loads_clipboard_helper_and_modal_copy_button() -> None:
    """base.html loads clipboard.js globally; the + modal's manifest editor
    carries a copy button (#m-copy)."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "/static/clipboard.js" in base
    assert 'id="m-copy"' in base


def test_modal_copy_button_copies_the_editor_text() -> None:
    """create_modal.js wires #m-copy to copy the manifest editor's full
    text through window.copyText."""
    js = (STATIC / "create_modal.js").read_text(encoding="utf-8")
    assert "m-copy" in js
    assert "window.copyText" in js
    assert "buffer.value" in js
