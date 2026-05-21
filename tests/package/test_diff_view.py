"""Tests for slice S09: collapsible diff view on the round page.

Source-level checks against the round template and the diff renderer —
runs without importing the runner since the runner's runtime deps
(fastapi, polars, etc.) are not in the package's dev-extras.

The round diff is a YAML-manifest diff: there is no syntax highlighting,
so diff_view.js paints only the per-line +/- prefix classes and wires the
copy buttons."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / 'experiment_runner'


def test_diff_view_module_exists() -> None:
    """The round-page hydration script paints the diff (#diff-source) with
    per-line classes and renders content as plain (escaped) text — the
    manifest is YAML, so no syntax highlighting."""
    js = PKG_DIR / 'static' / 'diff_view.js'
    assert js.is_file()
    text = js.read_text(encoding='utf-8')
    assert "getElementById('diff-source')" in text
    # No Python highlighter dependency — that module was retired with the
    # YAML-manifest migration.
    assert 'PythonHighlight' not in text


def test_round_page_frozen_manifest_pre_exists() -> None:
    """The frozen-manifest <pre> carries the id the copy button targets."""
    html = (PKG_DIR / 'templates' / 'round.html').read_text(encoding='utf-8')
    assert 'id="frozen-manifest"' in html


def test_round_page_uses_collapsible_diff_view() -> None:
    """The diff section is always rendered as a <details> block; it opens
    by default when diff_text is non-empty and stays closed when there is
    no diff. The diff content is hosted in an .atom-diff-view panel."""
    html = (PKG_DIR / 'templates' / 'round.html').read_text(encoding='utf-8')
    assert 'atom-diff-view' in html
    # Default-open driven by diff_text presence.
    assert '{% if diff_text %} open{% endif %}' in html
    # Server-rendered <pre> the diff_view.js script hydrates.
    assert 'id="diff-source"' in html
    # diff_view.js is loaded on the round page.
    assert '/static/diff_view.js' in html


def test_round_page_no_legacy_diff_pre() -> None:
    """The legacy <pre class="diff"> shape is gone — the slice's regression
    target. If a future PR re-introduces it, this test fails."""
    html = (PKG_DIR / 'templates' / 'round.html').read_text(encoding='utf-8')
    assert '<pre class="diff">' not in html


def test_diff_view_css_defined() -> None:
    """The .atom-diff-view rule mirrors the editor panel chrome (paper-2,
    accent left border, mono) and the per-line diff classes are present."""
    css = (PKG_DIR / 'static' / 'style.css').read_text(encoding='utf-8')
    assert '.atom-diff-view' in css
    for cls in ('.diff-add', '.diff-del', '.diff-hunk', '.diff-meta', '.diff-prefix'):
        assert cls in css, f'missing diff class: {cls}'
