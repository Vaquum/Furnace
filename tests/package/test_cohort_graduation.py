"""Tests for the sensor-to-cohort graduation slice: the operator selects
winning decoders from one round and writes a cohort definition.

`furnace/cohorts.py` is pure (no `limen`, no FastAPI), so it is tested
directly. The `/cohorts` route registration is asserted source-level
against `app.py` — the codebase's idiom (app.py can't be imported without
its full runtime; deploy/route invariants are pinned against source text).
"""

from __future__ import annotations

import json
from pathlib import Path

from furnace import cohorts

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / "furnace"

_MANIFEST_ID = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
_PIDS = ["c0ffee", "abc123", "beef99"]


def test_create_cohort_writes_definition_file(tmp_path: Path) -> None:
    """Writing a cohort definition lands one JSON file under
    `<root>/cohorts/`, named by the (folded) cohort_id."""
    cid = cohorts.write_cohort_definition(
        tmp_path, name="alpha", experiment_dir="/data/x/rounds/0001/limen_output",
        manifest_id=_MANIFEST_ID, permutation_ids=_PIDS,
    )
    files = list((tmp_path / "cohorts").glob("*.json"))
    assert len(files) == 1, files
    assert cid.startswith("sha256:")
    # filesystem-folded id is the filename stem; the canonical id is inside.
    assert files[0].stem == cid.replace(":", "_")


def test_cohort_definition_has_required_keys(tmp_path: Path) -> None:
    """The written definition carries exactly the keys a cohort consumer
    needs: cohort_id, name, experiment_dir, manifest_id, permutation_ids
    (the ids stored sorted, matching the cohort_id derivation)."""
    cohorts.write_cohort_definition(
        tmp_path, name="alpha", experiment_dir="/data/x/rounds/0001/limen_output",
        manifest_id=_MANIFEST_ID, permutation_ids=_PIDS,
    )
    definition = json.loads(next((tmp_path / "cohorts").glob("*.json")).read_text())
    assert set(definition) == {
        "cohort_id", "name", "experiment_dir", "manifest_id", "permutation_ids",
    }
    assert definition["name"] == "alpha"
    assert definition["manifest_id"] == _MANIFEST_ID
    assert definition["permutation_ids"] == sorted(_PIDS)


def test_cohort_id_matches_limen_formula() -> None:
    """Freeze ``compute_cohort_id``'s output against a *literal* hash so the
    actual bytes are pinned — re-deriving the value with the same json+sha256
    logic would be tautological. The literal below was computed from Limen's
    current formula (``limen.cohort.Cohort.set_members``: ``sha256:`` + SHA-256
    of the canonical ``{manifest_id, sorted permutation_ids}`` JSON); a change
    to either our replica or that formula makes this assertion fail."""
    expected = "sha256:15c8d648db04a4032d5d057b08cfbe60b2810e1a5580617bc4762f2fb070486e"
    assert cohorts.compute_cohort_id(_MANIFEST_ID, _PIDS) == expected
    # id is order-independent in the input (sorted internally)
    assert cohorts.compute_cohort_id(_MANIFEST_ID, list(reversed(_PIDS))) == expected


def test_cohort_rejects_duplicate_permutation_ids(tmp_path: Path) -> None:
    """Limen's ``Cohort`` rejects duplicate permutation_ids ('must be unique');
    a duplicate would hash to a cohort_id Limen could never produce. The writer
    mirrors that fail-loud contract rather than silently deduping."""
    try:
        cohorts.write_cohort_definition(
            tmp_path, name="dup", experiment_dir="/x",
            manifest_id=_MANIFEST_ID, permutation_ids=["a", "b", "a"],
        )
    except ValueError as exc:
        assert "unique" in str(exc)
    else:
        raise AssertionError("expected ValueError for duplicate permutation_ids")


def test_read_manifest_id_from_metadata(tmp_path: Path) -> None:
    """`read_manifest_id` pulls the round's manifest_id from
    limen_output/metadata.json."""
    (tmp_path / "metadata.json").write_text(json.dumps({"manifest_id": _MANIFEST_ID}))
    assert cohorts.read_manifest_id(tmp_path) == _MANIFEST_ID


def test_read_manifest_id_missing_fails(tmp_path: Path) -> None:
    """A round whose metadata has no manifest_id cannot be graduated — the
    grouping key would be ambiguous, so it fails loud rather than inventing one."""
    (tmp_path / "metadata.json").write_text(json.dumps({"sfd_module": "x"}))
    try:
        cohorts.read_manifest_id(tmp_path)
    except ValueError as exc:
        assert "manifest_id" in str(exc)
    else:
        raise AssertionError("expected ValueError for missing manifest_id")


def test_cohorts_route_registered() -> None:
    """app.py registers the graduation surface: GET /graduation, GET /cohorts,
    and POST /cohorts (the create handler), and the search page links to it."""
    app_text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    assert '@app.post("/cohorts")' in app_text
    assert '@app.get("/cohorts")' in app_text
    assert '@app.get("/graduation"' in app_text
    assert "cohorts.write_cohort_definition(" in app_text
    assert "from . import cohorts, inference" in app_text
    search_text = (PKG_DIR / "templates" / "search.html").read_text(encoding="utf-8")
    assert 'href="/graduation"' in search_text


def test_list_cohort_definitions(tmp_path: Path) -> None:
    """Listing returns every written definition; empty when none exist."""
    assert cohorts.list_cohort_definitions(tmp_path) == []
    cohorts.write_cohort_definition(
        tmp_path, name="a", experiment_dir="/x", manifest_id=_MANIFEST_ID,
        permutation_ids=["p1"],
    )
    listed = cohorts.list_cohort_definitions(tmp_path)
    assert len(listed) == 1
    assert listed[0]["name"] == "a"
