"""Tests for the manifest-editor syntax highlighting: the + / new modal's
YAML editor is a control surface, so the highlighting is vivid and
manifest-specific — `limen.*` callable paths and `{param}` sweep
placeholders, the manifest's own vocabulary, each carry their own colour.

Source-level checks against manifest_editor.js (the per-line tokenizer)
and style.css (the token palette)."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "furnace" / "static"


def test_tokenizer_tags_limen_callable_paths() -> None:
    """A `limen.*` dotted path — an indicator / feature / target /
    reference architecture the manifest wires in — is tagged `tok-limen`
    so it carries its own colour, distinct from a plain scalar."""
    js = (STATIC / "manifest_editor.js").read_text(encoding="utf-8")
    assert "tok-limen" in js
    assert "/^limen\\." in js


def test_tokenizer_tags_param_sweep_placeholders() -> None:
    """A `"{name}"` literal is a parameter-sweep placeholder — one of the
    manifest's substitution points — tagged `tok-param`, distinct from an
    ordinary quoted string."""
    js = (STATIC / "manifest_editor.js").read_text(encoding="utf-8")
    assert "tok-param" in js
    # the {...}-placeholder test inside the quoted-string branch
    assert "[^{}]+" in js


def test_token_palette_is_vivid_and_manifest_specific() -> None:
    """style.css colours every YAML token from the palette — the
    manifest-specific `tok-limen` (cyan) and `tok-param` (magenta)
    included. The two stars are not muted grey."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for cls in (
        "tok-key", "tok-limen", "tok-param", "tok-string", "tok-plain",
        "tok-number", "tok-keyword", "tok-comment", "tok-punc",
    ):
        assert f".{cls} " in css, f"missing token class .{cls}"
    limen = css[css.index(".tok-limen") :]
    limen = limen[: limen.index("}")]
    assert "var(--positive)" in limen or "#C4E8F4" in limen
    param = css[css.index(".tok-param") :]
    param = param[: param.index("}")]
    assert "#DC65A6" in param


def test_token_palette_is_consolidated() -> None:
    """The token palette is one block, owned by the manifest editor — the
    retired PythonHighlight-era token comment (the code-editor era) is
    gone, so a token colour has a single source of truth."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "shared PythonHighlight" not in css
