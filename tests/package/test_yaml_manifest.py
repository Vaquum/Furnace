"""Tests for the YAML-manifest migration: the experiment definition is no
longer a Python "SFD" code file (code.py + args.json) but a single Limen
native YAML manifest (manifest.yml).

Source-level checks — the codebase idiom: app.py / run_round.py cannot be
imported in the package's dev-extras (no fastapi / limen runtime deps), so
the tests read the source text and assert on it."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / "experiment_runner"
TEMPLATES = PKG_DIR / "templates"
STATIC = PKG_DIR / "static"


# ---- run_round.py ---------------------------------------------------------


def test_run_round_invokes_limen_cli() -> None:
    """run_round.py executes the round through the `limen` CLI — a real
    `limen run` subprocess on the round's manifest.yml — so the round's
    artefacts are exactly what the authoritative tool produced.
    experiment_runner never runs Limen in-process: there is no second
    pipeline that could drift from what the CLI does."""
    text = (PKG_DIR / "run_round.py").read_text(encoding="utf-8")
    # The `limen` console script is located and invoked as a subprocess.
    # run_round.py supervises that child via Popen — it forwards the round's
    # stop signal to limen and reaps it — rather than a blocking subprocess.run.
    assert "def _limen_cli(" in text
    assert "subprocess.Popen(" in text
    assert '"run", str(round_dir / "manifest.yml")' in text
    # No in-process Limen pipeline — the CLI is the only path in.
    assert re.search(r"(?m)^\s*(?:from|import)\s+limen\b", text) is None


def test_run_round_no_longer_execs_user_code() -> None:
    """The Python-SFD era exec-ed a user code file to obtain the SFD class.
    The manifest era has no user Python — assert the exec and the old
    code.py / args.json file names are gone from run_round.py."""
    text = (PKG_DIR / "run_round.py").read_text(encoding="utf-8")
    assert "exec(" not in text
    assert "code.py" not in text
    assert "args.json" not in text
    assert "sfd_identifier" not in text


# ---- app.py ---------------------------------------------------------------


def test_app_defines_starter_manifest() -> None:
    """A new experiment is seeded with STARTER_MANIFEST — a module-level
    YAML string carrying the __EXPERIMENT_NAME__ sentinel that
    create_experiment substitutes with the experiment id."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    assert "STARTER_MANIFEST" in text
    assert "__EXPERIMENT_NAME__" in text
    # It is a valid-shaped Limen manifest: the top-level blocks are present.
    assert "schema_version:" in text
    assert "sfd:" in text
    assert "uel:" in text


def test_app_defines_manifest_errors_helper() -> None:
    """_manifest_errors validates a manifest string by running the
    `limen validate` CLI — the same binary `limen run` enforces at
    round-start — and returns a flat error list parsed from its stdout.
    experiment_runner never validates a manifest in-process."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    assert "def _manifest_errors(" in text
    idx = text.index("def _manifest_errors(")
    body = text[idx:idx + 1600]
    assert '"validate"' in body
    assert "subprocess.run(" in body
    assert re.search(r"(?m)^\s*(?:from|import)\s+limen\b", body) is None


def test_create_experiment_seeds_manifest_yml() -> None:
    """create_experiment writes exp_dir/manifest.yml seeded from
    STARTER_MANIFEST — no more code.py + args.json seeding."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    idx = text.index("async def create_experiment(")
    body = text[idx:idx + 1500]
    assert '"manifest.yml"' in body
    assert 'STARTER_MANIFEST.replace("__EXPERIMENT_NAME__"' in body


def test_manifest_saved_via_put_only() -> None:
    """The manifest is written only by PUT /experiments/{id}/manifest — the
    creation modal's save path. There is no standalone GET manifest-editor
    route: editing happens solely in the modal. The PUT validates the body
    is a JSON object, runs _manifest_errors, and version-bumps atomically."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    assert re.search(r'@app\.put\("/experiments/\{experiment_id\}/manifest"', text)
    assert not re.search(r'@app\.get\("/experiments/\{experiment_id\}/manifest"', text)
    put_idx = text.index('@app.put("/experiments/{experiment_id}/manifest"')
    body = text[put_idx:put_idx + 2000]
    assert "isinstance(body, dict)" in body
    assert "_manifest_errors(manifest)" in body
    assert '"kind": "manifest_invalid"' in body
    assert "manifest.yml.tmp" in body
    assert "os.replace" in body
    assert "write_version_atomic" in body
    assert "status_code=409" in body


def test_start_round_snapshots_manifest_from_disk() -> None:
    """start_round reads the manifest from disk under the if_match flock,
    rejects an empty manifest, validates it via _manifest_errors, and
    snapshots it into the round dir as manifest.yml."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    idx = text.index('@app.post("/experiments/{experiment_id}/rounds"')
    body = text[idx:idx + 4000]
    assert 'manifest_path = exp_dir / "manifest.yml"' in body
    assert "manifest is empty" in body
    assert "_manifest_errors(manifest)" in body
    assert '(round_dir / "manifest.yml").write_text(manifest)' in body


def test_app_no_longer_references_python_sfd_storage() -> None:
    """The Python-SFD storage model is fully retired: no code.py, args.json,
    sfd_identifier, or DEFAULT_ARGS anywhere in app.py."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    for stale in ("code.py", "args.json", "sfd_identifier", "DEFAULT_ARGS"):
        assert stale not in text, f"stale Python-SFD reference {stale!r} in app.py"


def test_working_json_returns_manifest() -> None:
    """working.json returns the manifest text + version (no code / args)."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    idx = text.index("def working_json(")
    body = text[idx:idx + 400]
    assert '"manifest"' in body
    assert '"version"' in body


def test_round_views_guard_pre_migration_rounds() -> None:
    """A round created before the manifest migration has no manifest.yml
    (its definition was a frozen code.py + args.json). round_page and
    _compute_round_stats guard the read, so viewing or polling such a legacy
    round degrades gracefully instead of raising FileNotFoundError → 500."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    rp = text[text.index("def round_page(") :][:600]
    assert "manifest_path.exists() else" in rp
    cs = text[text.index("def _compute_round_stats(") :][:700]
    assert "if manifest_path.exists():" in cs


def test_search_index_set_excludes_stale_pre_migration_indices() -> None:
    """The search index set is built only from rounds `_ensure_round_index`
    currently vouches for — never a raw glob of `.index.parquet` files. A
    glob picks up a stale index left by a pre-migration round (no
    manifest.yml), and `_build_search_bundle` then 500s reading that round's
    absent manifest. `_ensure_indices` returns the collected
    `_ensure_round_index` results; the globbing `_index_paths` is gone."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    assert "def _index_paths(" not in text
    body = text[text.index("def _ensure_indices(") :][:1200]
    assert "def _ensure_indices(root: Path, experiment_names: list[str] | None) -> list[Path]:" in body
    assert "idx = _ensure_round_index(round_dir)" in body
    assert "if idx is not None:" in body


# ---- templates ------------------------------------------------------------


def test_manifest_editor_is_modal_only() -> None:
    """The manifest editor lives only inside the creation modal — there is
    no standalone manifest.html editor page (nor the retired code.html).
    manifest_editor.js, the highlighter the modal embeds, stays."""
    assert not (TEMPLATES / "manifest.html").exists()
    assert not (TEMPLATES / "code.html").exists()
    assert (STATIC / "manifest_editor.js").is_file()


def test_experiment_page_shows_frozen_manifest_not_an_editor() -> None:
    """The experiment view is a read-only mirror of the round viewer — a
    frozen-manifest section, no editor and no link to one (editing is
    modal-only). The retired Python-SFD args form stays gone."""
    html = (TEMPLATES / "experiment.html").read_text(encoding="utf-8")
    assert 'id="frozen-manifest"' in html
    assert "/experiments/{{ experiment_id }}/manifest" not in html
    assert "args-form" not in html
    assert "sfd_identifier" not in html


def test_creation_modal_saves_manifest_via_put() -> None:
    """The creation modal — the only manifest editor — saves through
    PUT /experiments/{id}/manifest and surfaces manifest_invalid errors
    returned in the 400 detail."""
    js = (STATIC / "create_modal.js").read_text(encoding="utf-8")
    assert "/manifest" in js
    assert "manifest_invalid" in js
