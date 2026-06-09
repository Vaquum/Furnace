"""Tests for one-active-cohort-per-bar-type with activation backfill
(slice S79 / #101).

Turning a cohort on makes it the sole active cohort for its arrow series and
backfills that series' served file. ``furnace/cohorts.py::activate_cohort`` (the
single-active rule) is pure and tested directly; the ``POST /cohorts/{url_id}/active``
route is exercised through a real app via ``TestClient``. The route's
``prediction_cache.backfill`` writes to the production ``/opt/conduit`` (its roots
are not injectable), so the route tests spy it to assert the activation wiring —
that the right cohort + series reach the backfill — rather than writing to /opt;
the file-writing itself is ``prediction_cache``'s own (separately tested) contract.
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


def test_activate_backfills_series_file(tmp_path: Path, monkeypatch) -> None:
    """Turning a cohort on through the route backfills its bar type's served
    file — the activated cohort's definition and its series reach backfill."""
    _auth_env(monkeypatch)
    root = tmp_path / "data"
    url_id = _make_cohort(root, tmp_path, "solo", "time_a", ["p1"])

    calls: list[tuple[dict[str, object], str]] = []

    def spy_backfill(definition, series):
        calls.append((definition, series))

    monkeypatch.setattr(prediction_cache, "backfill", spy_backfill)
    client = TestClient(create_app(root))

    r = client.post(f"/cohorts/{url_id}/active", json={"active": True}, auth=("u", "p"))

    assert r.status_code == 200
    assert len(calls) == 1
    definition, series = calls[0]
    assert series == "time_a"
    assert definition["active"] is True
    assert definition["permutation_ids"] == ["p1"]


def test_active_route_on_enforces_single_active(tmp_path: Path, monkeypatch) -> None:
    """End to end: POSTing active=true for cohort B on a series deactivates the
    prior active cohort A on that series, leaves B on, and backfills the series."""
    _auth_env(monkeypatch)
    root = tmp_path / "data"
    a = _make_cohort(root, tmp_path, "a", "time_x", ["p1"])
    b = _make_cohort(root, tmp_path, "b", "time_x", ["p2"])

    backfilled: list[str] = []
    monkeypatch.setattr(prediction_cache, "backfill", lambda definition, series: backfilled.append(series))
    client = TestClient(create_app(root))

    client.post(f"/cohorts/{a}/active", json={"active": True}, auth=("u", "p"))
    assert cohorts.find_cohort_definition(root, a)["active"] is True

    r = client.post(f"/cohorts/{b}/active", json={"active": True}, auth=("u", "p"))

    assert r.status_code == 200
    assert cohorts.find_cohort_definition(root, b)["active"] is True
    assert cohorts.find_cohort_definition(root, a)["active"] is False
    assert backfilled[-1] == "time_x"
