"""Tests for the round bundle endpoint: the zip returned by
GET /experiments/{eid}/rounds/{rid}/bundle is a useful archive of the
round's actual artefacts, not a near-empty 22 KB shell.

Source-level checks against app.py — the codebase's idiom (app.py can't
be imported without fastapi; the tests assert on source text).

The bundle had regressed in two ways the yaml-manifest refactor
introduced: (1) `iterdir()` is one-level, so a round whose `limen run`
artefacts sit in a timestamped subdirectory (legacy layout) produced a
near-empty zip; (2) the round-level frozen `manifest.yml` was dropped
from the bundle entirely. This test locks both back in — recursive walk
of `limen_output/`, plus the round-level manifest pinned at the bundle
root."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / "furnace"


def _bundle_body() -> str:
    """Return the source text of the `round_bundle` endpoint, from the
    `def` through the `return Response(...)` line — the rules-relevant
    window for the assertions below."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    start = text.index("def round_bundle(")
    end = text.index("return Response", start)
    return text[start:end]


def test_bundle_includes_round_level_manifest() -> None:
    """The round-level frozen `manifest.yml` — the canonical submission
    the operator pinned, which never gets rewritten — is included in the
    bundle. Without it the archive carries no self-describing record of
    what was run, and consumers can only infer the manifest from the
    limen-output copy (which may not exist for every layout)."""
    body = _bundle_body()
    assert 'round_dir / "manifest.yml"' in body, \
        "bundle must reference the round-level manifest path"
    assert "manifest_path.is_file()" in body, \
        "bundle must guard the manifest write on its existence"
    # The manifest is written into the zip under the bundle base.
    assert '"{base}/manifest.yml"' in body or "'{base}/manifest.yml'" in body, \
        "round-level manifest.yml must be written at the bundle root"


def test_bundle_walks_limen_output_recursively() -> None:
    """`limen run` can write artefacts straight into `limen_output/`
    (post-v0.17.1 layout) or into a timestamped subdirectory (legacy
    layout). The bundle walks `limen_output/` recursively so both layouts
    deliver every file. The previous one-level `iterdir()` silently
    dropped the subdirectory case, producing a near-empty zip — this
    test forbids regressing back to that."""
    body = _bundle_body()
    assert "limen_output.rglob(" in body, \
        "bundle must walk limen_output/ recursively (rglob)"
    assert "limen_output.iterdir()" not in body, \
        "iterdir() is one-level and misses subdirectory layouts"
