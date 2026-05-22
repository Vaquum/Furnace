"""Tests for the design-token alignment slice: experiment_runner's
stylesheet adopts the-lab's canonical design-token vocabulary 1:1.

The :root block carries the-lab token set verbatim — palette atoms,
surfaces, text, semantics, the --text-* composites, the --s-* rhythm
scale — and every rule references a semantic token or a composite, never
a legacy --ink/--paper/--gap-* name, never a --c-* palette atom, never a
bare palette hex. Source-level checks against style.css and the templates
(the codebase idiom — app.py can't be imported without fastapi, so the
tests assert on source text)."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / "experiment_runner"
STATIC = PKG_DIR / "static"
TEMPLATES = PKG_DIR / "templates"

# Token names retired by the alignment. Every one must be gone — from
# style.css rules and :root alike — or the two apps have drifted again
# into the parallel --ink/--paper vocabulary the alignment removed.
_LEGACY_TOKENS: tuple[str, ...] = (
    "--ink", "--ink-soft", "--ink-mute", "--ink-warm",
    "--paper", "--paper-2", "--rule",
    "--gap-s", "--gap-m", "--gap-l", "--base", "--motion",
    "--t-body", "--t-code", "--t-h", "--t-small", "--t-micro", "--t-title",
    "--positive", "--negative", "--active",
)

# the-lab's canonical :root names — the vocabulary both apps now share.
_CANON_TOKENS: tuple[str, ...] = (
    "--bg", "--highlight", "--border", "--border-subtle", "--border-strong",
    "--fg", "--fg-1", "--fg-2", "--fg-3",
    "--accent", "--pos", "--neg", "--danger",
    "--font-sans", "--font-mono", "--t", "--t-icon",
    "--leading-tight", "--leading-body", "--track-label",
    "--text-body", "--text-ui", "--text-label",
    "--text-data", "--text-data-em", "--text-code", "--text-icon",
    "--s-1", "--s-2", "--s-3", "--s-4", "--s-5", "--s-6",
    "--row-h", "--radius", "--border-w", "--border-w-accent",
    "--transition", "--control-h", "--icon-btn-w",
)

# Palette literals. These must appear only inside :root; a rule that
# names one has reached past the semantic layer.
_PALETTE_ATOMS: tuple[str, ...] = (
    "--c-near-black", "--c-warm-black", "--c-dark-gray", "--c-mid-gray",
    "--c-light-gray", "--c-pale-gray", "--c-off-white", "--c-pale-pink",
    "--c-pink", "--c-magenta", "--c-coral", "--c-lavender", "--c-yellow",
)

_ROLE_CLASSES: tuple[str, ...] = (
    "t-body", "t-ui", "t-label", "t-data", "t-code", "t-icon",
)

_TEXT_COMPOSITES: tuple[str, ...] = (
    "--text-body", "--text-ui", "--text-label",
    "--text-data", "--text-data-em", "--text-code", "--text-icon",
)


def _style_css() -> str:
    return (STATIC / "style.css").read_text(encoding="utf-8")


def _strip_comments(css: str) -> str:
    """CSS with /* ... */ comments removed. The absence checks below
    assert on what the rules reference — a comment that names a token or
    quotes a hex is prose, not a reference, and must not trip them."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _root_block(css: str) -> str:
    """The text of the first :root { ... } block, brace-balanced. Custom
    property values carry no braces, so a depth counter on { } isolates
    the block cleanly."""
    start = css.index(":root {")
    depth = 0
    for i in range(start, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[start : i + 1]
    raise AssertionError(":root block is not brace-balanced")


def _token_present(text: str, token: str) -> bool:
    """True if `token` appears as a complete custom-property name — not as
    the prefix of a longer one (so --t does not match --t-icon)."""
    return re.search(rf"{re.escape(token)}(?![\w-])", text) is not None


def test_root_defines_the_lab_canonical_tokens() -> None:
    """:root declares every token in the-lab's canonical set — palette
    atoms, surfaces, text, semantics, the --text-* composites and the
    --s-* rhythm scale — so a rule resolving a semantic token resolves to
    the same value it would in the-lab."""
    root = _root_block(_style_css())
    for tok in _PALETTE_ATOMS + _CANON_TOKENS:
        assert f"{tok}:" in root, f":root is missing the canonical token {tok}"


def test_no_legacy_token_names_survive() -> None:
    """The retired --ink/--paper/--gap-*/--motion/--t-* vocabulary appears
    nowhere in style.css — not in :root, not in a rule. A surviving legacy
    name means a rule still points at the old parallel universe."""
    css = _strip_comments(_style_css())
    for tok in _LEGACY_TOKENS:
        assert not _token_present(css, tok), f"legacy token {tok} still in style.css"


def test_palette_atoms_referenced_only_inside_root() -> None:
    """Rules reference semantic tokens, never a --c-* palette atom directly
    (design-system rule: one hue, one job, named through the semantic
    layer). Every --c-* occurrence lies inside the :root block."""
    css = _strip_comments(_style_css())
    outside = css.replace(_root_block(css), "", 1)
    for atom in _PALETTE_ATOMS:
        assert atom not in outside, f"palette atom {atom} referenced in a rule"


def test_no_bare_palette_hex_outside_root() -> None:
    """Palette literals (#RRGGBB) live only in :root. A bare hex in a rule
    is an off-system colour. Hex inside a data: URI (the select caret) is
    unavoidable embedded markup and exempt."""
    css = _strip_comments(_style_css())
    outside = css.replace(_root_block(css), "", 1)
    for line in outside.splitlines():
        if "data:image" in line:
            continue
        assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", line), \
            f"bare hex in a rule: {line.strip()}"


def test_role_classes_defined() -> None:
    """The six type role classes — the carriers of typography — are each
    defined so a template can wear a type role without a bare font rule."""
    css = _style_css()
    for cls in _ROLE_CLASSES:
        assert f".{cls} " in css, f"role class .{cls} not defined"


def test_text_composites_are_font_shorthands() -> None:
    """The --text-* composites are CSS `font` shorthands built from the
    type atoms — one declaration carrying family, size, weight, leading —
    so a rule sets typography with `font: var(--text-*)`, never a bare
    font-size / font-weight / line-height."""
    root = _root_block(_style_css())
    for comp in _TEXT_COMPOSITES:
        line = next(
            (ln for ln in root.splitlines() if ln.strip().startswith(f"{comp}:")),
            "",
        )
        assert "var(--t" in line and "var(--font-" in line, \
            f"{comp} is not a composite over the type atoms"


def test_templates_carry_no_legacy_tokens() -> None:
    """Inline styles and JS color strings in the templates use the canonical
    vocabulary — no legacy --ink/--negative/etc. survives an inline style
    or a `.style.color =` assignment."""
    for tpl in sorted(TEMPLATES.glob("*.html")):
        text = tpl.read_text(encoding="utf-8")
        for tok in _LEGACY_TOKENS:
            assert not _token_present(text, tok), \
                f"legacy token {tok} in {tpl.name}"


def test_static_js_carries_no_legacy_tokens() -> None:
    """The static JS sets a few colours as `var(--token)` strings; those
    too resolve to the canonical vocabulary."""
    for js in sorted(STATIC.glob("*.js")):
        text = js.read_text(encoding="utf-8")
        for tok in _LEGACY_TOKENS:
            assert not _token_present(text, tok), \
                f"legacy token {tok} in {js.name}"


def test_base_html_loads_only_400_500_weights() -> None:
    """The type system uses two weights — 400 and 500. base.html loads
    exactly those from Google Fonts; the retired 600 weight is gone, so a
    rule cannot reach for a weight the composites do not define."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "wght@400;500" in base
    assert ";600" not in base
