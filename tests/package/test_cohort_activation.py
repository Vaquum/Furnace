"""Tests for one-active-cohort-per-bar-type activation.

Turning a cohort on makes it the sole active cohort for its arrow series. The
``POST /cohorts/{url_id}/active`` route is exercised through a real app via
``TestClient`` and must not run prediction backfill in the web request; the
separate scheduler process owns prediction refresh work.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from furnace import cohorts, prediction_cache
from furnace.app import create_app

_MANIFEST_ID = "sha256:" + "1" * 64


def _arrow_metadata(series: str) -> dict[str, object]:
    """A ``metadata.json`` whose data_source is an arrow ``series`` — resolvable
    by both ``cohort_display_meta`` (arrow_series) and ``series_for_cohort``."""
    return {
        "yaml_reference": {
            "sfd": {"manifest": {"data_source": {
                "method": "limen.data.HistoricalData.get_arrow_file",
                "params": {"file_path": f"/opt/arrow/{series}/latest.arrow"},
            }}}
        }
    }


def _make_cohort(root: Path, tmp_path: Path, name: str, series: str, perms: list[str]) -> str:
    """Create a cohort on ``series`` and return its url_id. Distinct ``perms``
    give distinct cohort_ids, so two cohorts can share one series."""
    exp = tmp_path / name
    exp.mkdir()
    (exp / "metadata.json").write_text(json.dumps(_arrow_metadata(series)), encoding="utf-8")
    cid = cohorts.write_cohort_definition(
        root, name=name, cohort_type="direction",
        experiment_dir=str(exp), manifest_id=_MANIFEST_ID, permutation_ids=perms,
    )
    return cid.replace(":", "_")


def _auth_env(monkeypatch) -> None:
    monkeypatch.setenv("APP_USERNAME", "u")
    monkeypatch.setenv("APP_PASSWORD", "p")
    monkeypatch.setenv("API_SECRET", "s")


def test_activate_sets_self_active(tmp_path: Path) -> None:
    """activate_cohort turns the named cohort on (and persists it)."""
    root = tmp_path / "data"
    url_id = _make_cohort(root, tmp_path, "solo", "time_a", ["p1"])

    definition = cohorts.activate_cohort(root, url_id)

    assert definition["active"] is True
    persisted = cohorts.find_cohort_definition(root, url_id)
    assert persisted is not None and persisted["active"] is True


def test_activate_deactivates_same_series_peer(tmp_path: Path) -> None:
    """Activating one cohort flips off another cohort on the same arrow series —
    one active cohort per bar type."""
    root = tmp_path / "data"
    a = _make_cohort(root, tmp_path, "a", "time_x", ["p1"])
    b = _make_cohort(root, tmp_path, "b", "time_x", ["p2"])
    cohorts.set_cohort_active(root, a, True)

    cohorts.activate_cohort(root, b)

    assert cohorts.find_cohort_definition(root, b)["active"] is True
    assert cohorts.find_cohort_definition(root, a)["active"] is False


def test_activate_leaves_other_series_untouched(tmp_path: Path) -> None:
    """A cohort on a different arrow series is not affected — the single-active
    rule is per bar type, not global."""
    root = tmp_path / "data"
    a = _make_cohort(root, tmp_path, "a", "time_x", ["p1"])
    other = _make_cohort(root, tmp_path, "other", "time_y", ["p2"])
    cohorts.set_cohort_active(root, other, True)

    cohorts.activate_cohort(root, a)

    assert cohorts.find_cohort_definition(root, a)["active"] is True
    assert cohorts.find_cohort_definition(root, other)["active"] is True


def test_active_route_does_not_backfill_in_web_request(tmp_path: Path, monkeypatch) -> None:
    """Turning a cohort on through the route persists active state only. It
    must not run prediction backfill in the web request, because sensor binding
    can be CPU-heavy and failure-prone; the scheduler process owns refresh."""
    _auth_env(monkeypatch)
    root = tmp_path / "data"
    url_id = _make_cohort(root, tmp_path, "solo", "time_a", ["p1"])

    def fail_backfill(definition, series):
        raise AssertionError("active route must not backfill")

    monkeypatch.setattr(prediction_cache, "backfill", fail_backfill)
    client = TestClient(create_app(root))

    r = client.post(f"/cohorts/{url_id}/active", json={"active": True}, auth=("u", "p"))

    assert r.status_code == 200
    assert cohorts.find_cohort_definition(root, url_id)["active"] is True


def test_active_route_on_enforces_single_active(tmp_path: Path, monkeypatch) -> None:
    """End to end: POSTing active=true for cohort B on a series deactivates the
    prior active cohort A on that series and leaves B on."""
    _auth_env(monkeypatch)
    root = tmp_path / "data"
    a = _make_cohort(root, tmp_path, "a", "time_x", ["p1"])
    b = _make_cohort(root, tmp_path, "b", "time_x", ["p2"])

    client = TestClient(create_app(root))

    client.post(f"/cohorts/{a}/active", json={"active": True}, auth=("u", "p"))
    assert cohorts.find_cohort_definition(root, a)["active"] is True

    r = client.post(f"/cohorts/{b}/active", json={"active": True}, auth=("u", "p"))

    assert r.status_code == 200
    assert cohorts.find_cohort_definition(root, b)["active"] is True
    assert cohorts.find_cohort_definition(root, a)["active"] is False
