"""Tests for the per-cohort on/off switch (slice S78 / #100).

A cohort is now created off; an operator turns it on, and the per-minute
scheduler refreshes only active cohorts. ``furnace/cohorts.py`` (the flag +
``set_cohort_active``) and ``furnace/cache_scheduler.py`` (the skip) are pure and
tested directly; the ``POST /cohorts/{url_id}/active`` route and the COHORTS-page
toggle are exercised through a real app via ``TestClient``.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from furnace import cache_scheduler, cohorts, prediction_cache
from furnace.app import create_app

_MANIFEST_ID = "sha256:" + "1" * 64


def _arrow_metadata(series: str) -> dict[str, object]:
    """The minimal ``metadata.json`` ``series_for_cohort`` walks to resolve a
    cohort's ``/opt/arrow`` series."""
    return {
        "yaml_reference": {
            "sfd": {"manifest": {"data_source": {"params": {
                "file_path": f"/x/opt/arrow/{series}/latest.arrow"
            }}}}
        }
    }


def _auth_env(monkeypatch) -> None:
    monkeypatch.setenv("APP_USERNAME", "u")
    monkeypatch.setenv("APP_PASSWORD", "p")
    monkeypatch.setenv("API_SECRET", "s")


def test_new_cohort_is_inactive_by_default(tmp_path: Path) -> None:
    """A freshly written cohort definition is off — the operator must turn it on
    before the scheduler serves it."""
    cohorts.write_cohort_definition(
        tmp_path, name="a", cohort_type="direction", experiment_dir="/x",
        manifest_id=_MANIFEST_ID, permutation_ids=["p1"],
    )
    definition = json.loads(next((tmp_path / "cohorts").glob("*.json")).read_text())
    assert definition["active"] is False


def test_set_cohort_active_persists_flag(tmp_path: Path) -> None:
    """``set_cohort_active`` flips the stored ``active`` flag and returns the new
    value, resolving the file by url_id (never building a path from it)."""
    cid = cohorts.write_cohort_definition(
        tmp_path, name="a", cohort_type="direction", experiment_dir="/x",
        manifest_id=_MANIFEST_ID, permutation_ids=["p1"],
    )
    url_id = cid.replace(":", "_")

    assert cohorts.set_cohort_active(tmp_path, url_id, True) is True
    assert json.loads(next((tmp_path / "cohorts").glob("*.json")).read_text())["active"] is True

    assert cohorts.set_cohort_active(tmp_path, url_id, False) is False
    assert json.loads(next((tmp_path / "cohorts").glob("*.json")).read_text())["active"] is False


def test_refresh_once_skips_inactive_cohort(tmp_path: Path, monkeypatch) -> None:
    """``refresh_once`` tops up only active cohorts: an inactive one with a valid
    arrow series is skipped entirely (its ``top_up`` is never called), while an
    active one on a different series is served."""
    root = tmp_path / "data"
    cohorts_dir = root / "cohorts"
    cohorts_dir.mkdir(parents=True)
    for name, series, active in (("on", "time_on", True), ("off", "time_off", False)):
        exp = tmp_path / name
        exp.mkdir()
        (exp / "metadata.json").write_text(json.dumps(_arrow_metadata(series)), encoding="utf-8")
        (cohorts_dir / f"{name}.json").write_text(
            json.dumps({
                "cohort_id": f"sha256:{name}", "name": name,
                "experiment_dir": str(exp), "permutation_ids": ["p"], "active": active,
            }),
            encoding="utf-8",
        )

    served: list[str] = []

    def spy_top_up(cohort_def, series, conduit_root, arrow_root):
        served.append(series)
        return 1

    monkeypatch.setattr(prediction_cache, "top_up", spy_top_up)

    res = cache_scheduler.refresh_once(root, tmp_path / "conduit", tmp_path / "arrow")

    assert "on" in res
    assert "off" not in res
    assert served == ["time_on"]


def test_active_route_flips_flag(tmp_path: Path, monkeypatch) -> None:
    """POST /cohorts/{url_id}/active flips and persists the stored flag, echoes
    the new state, and 404s for a cohort that does not exist."""
    _auth_env(monkeypatch)
    cid = cohorts.write_cohort_definition(
        tmp_path, name="a", cohort_type="direction", experiment_dir="/x",
        manifest_id=_MANIFEST_ID, permutation_ids=["p1"],
    )
    url_id = cid.replace(":", "_")
    client = TestClient(create_app(tmp_path))

    r = client.post(f"/cohorts/{url_id}/active", json={"active": True}, auth=("u", "p"))
    assert r.status_code == 200
    assert r.json() == {"url_id": url_id, "active": True}
    assert json.loads(next((tmp_path / "cohorts").glob("*.json")).read_text())["active"] is True

    missing = client.post("/cohorts/sha256_nope/active", json={"active": True}, auth=("u", "p"))
    assert missing.status_code == 404


def test_cohorts_page_renders_toggle_control(tmp_path: Path, monkeypatch) -> None:
    """The COHORTS page renders a per-row on/off switch carrying the cohort's
    url_id and its current (off-by-default) state."""
    _auth_env(monkeypatch)
    cid = cohorts.write_cohort_definition(
        tmp_path, name="a", cohort_type="direction", experiment_dir="/x",
        manifest_id=_MANIFEST_ID, permutation_ids=["p1"],
    )
    url_id = cid.replace(":", "_")
    client = TestClient(create_app(tmp_path))

    r = client.get("/cohorts", auth=("u", "p"))
    assert r.status_code == 200
    html = r.text
    assert 'class="cohort-active"' in html
    assert 'role="switch"' in html
    assert f'data-cohort-url="{url_id}"' in html
    assert 'aria-checked="false"' in html  # off by default
