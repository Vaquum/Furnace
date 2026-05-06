"""Tests for slice S09: code-editor-styled diff view on the round page.

Source-level checks against the round template, the diff renderer, and the
shared Python highlighter — runs without importing the runner since the
runner's runtime deps (fastapi, polars, etc.) are not in the package's
dev-extras."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / 'experiment_runner'


def test_python_highlight_module_exists() -> None:
    """The shared Python highlighter is its own file (loaded from base.html)
    so the code editor and the diff view paint tokens identically."""
    js = PKG_DIR / 'static' / 'python_highlight.js'
    assert js.is_file()
    text = js.read_text(encoding='utf-8')
    assert 'window.PythonHighlight' in text
    assert 'highlightPython' in text


def test_diff_view_module_exists() -> None:
    """The round-page hydration script paints the diff (#diff-source) and
    the frozen-code snapshot (#frozen-code), both using the shared
    PythonHighlight tokenizer."""
    js = PKG_DIR / 'static' / 'diff_view.js'
    assert js.is_file()
    text = js.read_text(encoding='utf-8')
    assert "getElementById('diff-source')" in text
    assert "getElementById('frozen-code')" in text
    # Uses the shared highlighter, not its own copy.
    assert 'window.PythonHighlight' in text


def test_round_page_frozen_code_has_highlight_target() -> None:
    """The frozen-code <pre> carries the id the round-page script looks for,
    so the snapshot reads the same as the editor's live buffer."""
    html = (PKG_DIR / 'templates' / 'round.html').read_text(encoding='utf-8')
    assert 'id="frozen-code"' in html


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


def test_base_html_loads_python_highlighter() -> None:
    """python_highlight.js must be loaded from the base template so every
    page (code editor and round/diff view alike) has it in scope."""
    html = (PKG_DIR / 'templates' / 'base.html').read_text(encoding='utf-8')
    assert '/static/python_highlight.js' in html


def test_code_editor_uses_shared_highlighter() -> None:
    """The editor delegates tokenization to window.PythonHighlight rather
    than carrying its own copy."""
    js = (PKG_DIR / 'static' / 'code_editor.js').read_text(encoding='utf-8')
    assert 'window.PythonHighlight.highlightPython' in js
    # The duplicated tokenizer is gone.
    assert 'PY_TOKENIZER = new RegExp' not in js
    assert 'PY_KEYWORDS = new Set' not in js
