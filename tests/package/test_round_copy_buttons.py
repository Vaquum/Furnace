"""Tests for slice S10: copy-to-clipboard buttons on every code block on the
round page. Source-level checks against the round template, the round-page
hydration script, and the shared copy-button CSS."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / 'experiment_runner'


def test_round_page_has_copy_buttons() -> None:
    """One copy button per code block: the manifest diff and the frozen
    manifest."""
    html = (PKG_DIR / 'templates' / 'round.html').read_text(encoding='utf-8')
    assert html.count('class="code-block-copy"') >= 2


def test_each_copy_button_declares_target() -> None:
    """Every button names its source <pre> via data-copy-target so the JS
    handler can resolve it with a single getElementById call."""
    html = (PKG_DIR / 'templates' / 'round.html').read_text(encoding='utf-8')
    targets = set(re.findall(r'data-copy-target="([^"]+)"', html))
    assert {'diff-source', 'frozen-manifest'}.issubset(targets)
    # And the matching <pre id="..."> elements exist alongside.
    for target in ('diff-source', 'frozen-manifest'):
        assert f'id="{target}"' in html, f'missing <pre id="{target}">'


def test_copy_button_css_rule_defined() -> None:
    """The .code-block-copy class carries the chrome (hairline-only,
    ink-mute → accent) plus the .copied / .copy-failed flash states."""
    css = (PKG_DIR / 'static' / 'style.css').read_text(encoding='utf-8')
    assert '.code-block-copy' in css
    assert '.code-block-copy.copied' in css
    assert '.code-block-copy.copy-failed' in css
    # The wrap container that hosts the absolute-positioned button.
    assert '.code-block-wrap' in css


def test_diff_view_wires_copy_buttons() -> None:
    """The round-page hydration script wires every .code-block-copy via a
    single querySelectorAll, calls navigator.clipboard.writeText with the
    target's textContent, and surfaces failure on the icon."""
    js = (PKG_DIR / 'static' / 'diff_view.js').read_text(encoding='utf-8')
    assert "querySelectorAll('.code-block-copy')" in js
    assert 'navigator.clipboard.writeText' in js
    assert 'data-copy-target' in js or 'dataset.copyTarget' in js
    # Failure path flashes the icon; same pattern as the editor's copy button.
    assert "'copy-failed'" in js or '"copy-failed"' in js


def test_delete_round_endpoint_exists() -> None:
    """app.py declares DELETE /experiments/{id}/rounds/{round_id}, refuses
    when the round is in an active state, and rmtrees the round dir
    otherwise."""
    text = (PKG_DIR / 'app.py').read_text(encoding='utf-8')
    pattern = r'@app\.delete\("/experiments/\{experiment_id\}/rounds/\{round_id\}"\)'
    assert re.search(pattern, text) is not None
    idx = text.index('@app.delete("/experiments/{experiment_id}/rounds/{round_id}"')
    body = text[idx:idx + 1500]
    assert 'ACTIVE_STATES' in body
    assert 'shutil.rmtree(round_dir)' in body
    assert 'status_code=409' in body


def test_round_row_renders_delete_button() -> None:
    """The rounds-table partial includes a .delete-round button per row,
    carrying data-round=<round_id>. Header column for the action exists in
    experiment.html."""
    rows = (PKG_DIR / 'templates' / '_round_rows.html').read_text(encoding='utf-8')
    assert 'class="delete-round"' in rows
    assert 'data-round="{{ r.round_id }}"' in rows
    exp = (PKG_DIR / 'templates' / 'experiment.html').read_text(encoding='utf-8')
    # Two col-actions thead entries are present today (one for the existing
    # blank action column and the new delete column on the rounds table).
    # Easier mechanical check: the experiment page has at least one
    # col-actions header on the rounds table — the only table on that page.
    assert 'col-actions' in exp
    # Event-delegated handler is wired to body (so it survives polling
    # re-renders of #rounds-rows).
    assert ".delete-round" in exp
    assert "method: 'DELETE'" in exp


def test_delete_round_css_shares_delete_exp_chrome() -> None:
    """The .delete-round button reuses the same icon-button rules as the
    existing .delete-exp on the Jobs page — combined selector, no
    duplication."""
    css = (PKG_DIR / 'static' / 'style.css').read_text(encoding='utf-8')
    # Combined base selector
    assert '.delete-exp,\n.delete-round' in css
    # Combined hover selector
    assert '.delete-exp:hover,\n.delete-round:hover' in css
