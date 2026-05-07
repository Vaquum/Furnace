"""Tests for slice S10: copy-to-clipboard buttons on every code block on the
round page. Source-level checks against the round template, the round-page
hydration script, and the shared copy-button CSS."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / 'experiment_runner'


def test_round_page_has_three_copy_buttons() -> None:
    """One copy button per code block: diff, frozen args, frozen code."""
    html = (PKG_DIR / 'templates' / 'round.html').read_text(encoding='utf-8')
    assert html.count('class="code-block-copy"') >= 3


def test_each_copy_button_declares_target() -> None:
    """Every button names its source <pre> via data-copy-target so the JS
    handler can resolve it with a single getElementById call."""
    html = (PKG_DIR / 'templates' / 'round.html').read_text(encoding='utf-8')
    targets = set(re.findall(r'data-copy-target="([^"]+)"', html))
    assert {'diff-source', 'frozen-args', 'frozen-code'}.issubset(targets)
    # And the matching <pre id="..."> elements exist alongside.
    for target in ('diff-source', 'frozen-args', 'frozen-code'):
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
