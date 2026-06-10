"""Tests for the cohort lifecycle: the operator selects one round from
search, creates a named cohort, and browses it (a cohort is one round's
permutations, possibly filtered).

`furnace/cohorts.py` is pure (no `limen`, no FastAPI), so it is tested
directly. The `/cohorts` route registration is asserted source-level against
`app.py` — the codebase's idiom (app.py can't be imported without its full
runtime; deploy/route invariants are pinned against source text).
"""

from __future__ import annotations

import json
import re
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
        tmp_path, name="alpha", cohort_type="direction",
        experiment_dir="/data/x/rounds/0001/limen_output",
        manifest_id=_MANIFEST_ID, permutation_ids=_PIDS,
    )
    files = list((tmp_path / "cohorts").glob("*.json"))
    assert len(files) == 1, files
    assert cid.startswith("sha256:")
    # filesystem-folded id is the filename stem; the canonical id is inside.
    assert files[0].stem == cid.replace(":", "_")


def test_cohort_definition_has_required_keys(tmp_path: Path) -> None:
    """The written definition carries exactly the keys a cohort consumer
    needs: cohort_id, name, cohort_type, experiment_dir, manifest_id,
    permutation_ids (the ids stored sorted, matching the cohort_id
    derivation), and active (off at creation — an operator turns it on)."""
    cohorts.write_cohort_definition(
        tmp_path, name="alpha", cohort_type="magnitude",
        experiment_dir="/data/x/rounds/0001/limen_output",
        manifest_id=_MANIFEST_ID, permutation_ids=_PIDS,
    )
    definition = json.loads(next((tmp_path / "cohorts").glob("*.json")).read_text())
    assert set(definition) == {
        "cohort_id", "name", "cohort_type", "experiment_dir",
        "manifest_id", "permutation_ids", "active",
    }
    assert definition["name"] == "alpha"
    assert definition["cohort_type"] == "magnitude"
    assert definition["manifest_id"] == _MANIFEST_ID
    assert definition["permutation_ids"] == sorted(_PIDS)
    assert definition["active"] is False


def test_cohort_type_must_be_direction_or_magnitude(tmp_path: Path) -> None:
    """cohort_type is the operator's classification; an unknown value fails
    loud rather than being stored as-is."""
    try:
        cohorts.write_cohort_definition(
            tmp_path, name="x", cohort_type="sideways", experiment_dir="/x",
            manifest_id=_MANIFEST_ID, permutation_ids=["p1"],
        )
    except ValueError as exc:
        assert "cohort_type" in str(exc)
    else:
        raise AssertionError("expected ValueError for an unknown cohort_type")


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
            tmp_path, name="dup", cohort_type="direction", experiment_dir="/x",
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
    """A round whose metadata has no manifest_id cannot form a cohort — the
    grouping key would be ambiguous, so it fails loud rather than inventing one."""
    (tmp_path / "metadata.json").write_text(json.dumps({"sfd_module": "x"}))
    try:
        cohorts.read_manifest_id(tmp_path)
    except ValueError as exc:
        assert "manifest_id" in str(exc)
    else:
        raise AssertionError("expected ValueError for missing manifest_id")


def test_list_cohort_definitions(tmp_path: Path) -> None:
    """Listing returns every written definition; empty when none exist."""
    assert cohorts.list_cohort_definitions(tmp_path) == []
    cohorts.write_cohort_definition(
        tmp_path, name="a", cohort_type="direction", experiment_dir="/x",
        manifest_id=_MANIFEST_ID, permutation_ids=["p1"],
    )
    listed = cohorts.list_cohort_definitions(tmp_path)
    assert len(listed) == 1
    assert listed[0]["name"] == "a"


def test_delete_cohort_definition(tmp_path: Path) -> None:
    """Deleting by cohort_id removes the file; deleting an absent id is a
    no-op that reports False rather than raising."""
    cid = cohorts.write_cohort_definition(
        tmp_path, name="a", cohort_type="direction", experiment_dir="/x",
        manifest_id=_MANIFEST_ID, permutation_ids=["p1"],
    )
    assert cohorts.delete_cohort_definition(tmp_path, cid) is True
    assert cohorts.list_cohort_definitions(tmp_path) == []
    assert cohorts.delete_cohort_definition(tmp_path, cid) is False


def test_delete_cohort_rejects_path_traversal(tmp_path: Path) -> None:
    """A crafted cohort_id cannot unlink a .json file outside <root>/cohorts/:
    the deletion resolves by enumerating the cohorts dir, never by building a
    path from the id, so `../victim` matches nothing and the victim survives."""
    victim = tmp_path / "victim.json"          # a reachable file outside cohorts/
    victim.write_text("{}")
    cohorts.write_cohort_definition(
        tmp_path, name="a", cohort_type="direction", experiment_dir="/x",
        manifest_id=_MANIFEST_ID, permutation_ids=["p1"],
    )
    assert cohorts.delete_cohort_definition(tmp_path, "../victim") is False
    assert victim.exists()


def test_find_cohort_definition_by_url_id(tmp_path: Path) -> None:
    """`find_cohort_definition` resolves a cohort by its URL id (the folded
    cohort_id) without building a path from the input; an unknown id is None."""
    cid = cohorts.write_cohort_definition(
        tmp_path, name="a", cohort_type="direction", experiment_dir="/x",
        manifest_id=_MANIFEST_ID, permutation_ids=["p1"],
    )
    url_id = cid.replace(":", "_")
    found = cohorts.find_cohort_definition(tmp_path, url_id)
    assert found is not None and found["cohort_id"] == cid
    assert cohorts.find_cohort_definition(tmp_path, "sha256_deadbeef") is None


def test_cohort_display_meta_exposes_url_id_and_type(tmp_path: Path) -> None:
    """The display meta a cohort page renders carries the URL id (folded
    cohort_id), the operator type, and the decoder count."""
    cid = cohorts.write_cohort_definition(
        tmp_path, name="a", cohort_type="magnitude", experiment_dir="/x",
        manifest_id=_MANIFEST_ID, permutation_ids=["p1", "p2"],
    )
    meta = cohorts.cohort_display_meta(
        next(c for c in cohorts.list_cohort_definitions(tmp_path) if c["cohort_id"] == cid)
    )
    assert meta["url_id"] == cid.replace(":", "_")
    assert meta["cohort_type"] == "magnitude"
    assert meta["decoder_count"] == 2


def test_cohort_routes_registered() -> None:
    """app.py registers the cohort surface — the list, create, delete, and the
    detail/drilldown pages."""
    app_text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    assert '@app.get("/cohorts"' in app_text
    assert '@app.post("/cohorts")' in app_text
    assert '@app.delete("/cohorts/{cohort_id:path}")' in app_text
    assert '@app.get("/cohorts/{url_id}"' in app_text
    assert '@app.get("/cohorts/{url_id}/results"' in app_text
    assert "cohorts.write_cohort_definition(" in app_text
    # cohorts is imported from the package (isort may merge the import line).
    assert re.search(r"from \. import [\w, ]*\bcohorts\b", app_text)


def test_search_links_cohort_create() -> None:
    """The search page's cohort affordance is the create-cohort control, and
    the cohorts table links each row into its detail page."""
    search_text = (PKG_DIR / "templates" / "search.html").read_text(encoding="utf-8")
    assert 'id="create-cohort-btn"' in search_text
    cohorts_text = (PKG_DIR / "templates" / "cohorts.html").read_text(encoding="utf-8")
    assert "/cohorts/{{ c.url_id }}" in cohorts_text


def test_no_graduation_wording_anywhere() -> None:
    """The graduation view died in v0.29.0; the vocabulary died with this
    slice. Cohorts are created, activated, and served — nothing "graduates".
    Self-enforcing: any file under furnace/ or tests/ that reintroduces the
    word fails this test."""
    roots = (PKG_DIR, REPO_ROOT / "tests")
    offenders: list[str] = []
    for root in roots:
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".html", ".css", ".js"} or "__pycache__" in path.parts:
                continue
            if path.resolve() == Path(__file__).resolve():
                continue  # this test names the word to ban it
            if "graduat" in path.read_text(encoding="utf-8").lower():
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


def test_legacy_inference_routes_removed() -> None:
    """The v0.21-era per-round inference surface (cohort/input, cohort/predict)
    is gone — it re-trained every member per request and is superseded by
    Conduit serving (/opt/conduit + the serving manifest). app.py neither
    registers the routes nor imports furnace.inference."""
    app_text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    assert "cohort/input" not in app_text
    assert "cohort/predict" not in app_text
    assert not re.search(r"from \. import [\w, ]*\binference\b", app_text)
    # inference.py remains as the limen boundary the serving path imports through.
    inference_text = (PKG_DIR / "inference.py").read_text(encoding="utf-8")
    for symbol in ("Cohort", "HistoricalData", "Trainer"):
        assert symbol in inference_text


def test_create_cohort_rejects_multiple_rounds(tmp_path: Path, monkeypatch) -> None:
    """A cohort is created from exactly ONE round — the served history must
    never mix results of different runs. Two rounds in the selection 400 before
    any round dir is touched; the error names the rule."""
    from fastapi.testclient import TestClient

    from furnace.app import create_app

    monkeypatch.setenv("APP_USERNAME", "u")
    monkeypatch.setenv("APP_PASSWORD", "p")
    monkeypatch.setenv("API_SECRET", "s")
    client = TestClient(create_app(tmp_path))

    r = client.post(
        "/cohorts",
        json={
            "name": "two-rounds",
            "cohort_type": "direction",
            "rounds": [
                {"experiment_id": "exp", "round_id": "0001"},
                {"experiment_id": "exp", "round_id": "0002"},
            ],
            "filters": {},
        },
        auth=("u", "p"),
    )
    assert r.status_code == 400
    assert "exactly one round" in r.json()["detail"]
