"""Tests for S91 (PRD #137): mutation endpoints run in the threadpool.

Two kinds of proof. The loop tests drive the ASGI app on a single event loop
(httpx ``ASGITransport``) with the slow part stubbed: while a manifest save
validates / a cohort create rebuilds indices in a worker thread, ``/health``
must answer immediately — before the fix the whole loop (and every other
request) waited. The race tests hammer the two guards whose safety previously
rested on the loop's accidental serialization: same-name experiment creation
(atomic mkdir claim) and the one-active-per-series cohort flip (in-process
lock), both under real thread concurrency.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

import furnace.app as appmod
from furnace import cohorts
from furnace.app import create_app

AUTH = ("u", "p")
_MANIFEST_ID = "sha256:" + "1" * 64


def _auth_env(monkeypatch) -> None:
    monkeypatch.setenv("APP_USERNAME", "u")
    monkeypatch.setenv("APP_PASSWORD", "p")
    monkeypatch.setenv("API_SECRET", "s")


def _round_with_metadata(root: Path) -> None:
    """experiments/e1/rounds/0001 with a manifest_id-bearing limen_output —
    just enough on-disk round for create_cohort to reach the perm-matching
    step (which the loop test stubs slow)."""
    limen_output = root / "experiments" / "e1" / "rounds" / "0001" / "limen_output"
    limen_output.mkdir(parents=True)
    (limen_output / "metadata.json").write_text(json.dumps({"manifest_id": _MANIFEST_ID}))


def _run_with_health_probe(app, fire) -> tuple[float, bool, object]:
    """On ONE event loop: start `fire(client)` as a task, give it 0.4 s to get
    into its (stubbed, 2.5 s) blocking section, then GET /health. Returns the
    wall time from task start to the health response, whether the slow request
    was genuinely still in flight when health answered, and its response."""

    async def main() -> tuple[float, bool, object]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            t0 = time.monotonic()
            slow_task = asyncio.create_task(fire(client))
            await asyncio.sleep(0.4)
            health = await client.get("/health")
            elapsed = time.monotonic() - t0
            in_flight = not slow_task.done()
            assert health.status_code == 200
            slow = await slow_task
            return elapsed, in_flight, slow

    return asyncio.run(main())


def test_health_answers_while_manifest_save_validates(tmp_path: Path, monkeypatch) -> None:
    """A save whose `limen validate` takes 2.5 s must not freeze the loop:
    /health answers in well under the validate time, while the save is still
    in flight — then the save itself completes normally."""
    _auth_env(monkeypatch)
    monkeypatch.setattr(appmod, "_manifest_errors", lambda _m: time.sleep(2.5) or [])
    app = create_app(tmp_path)
    eid = TestClient(app).post("/experiments", json={"name": "exp"}, auth=AUTH).json()["id"]

    async def fire(client: httpx.AsyncClient):
        return await client.put(
            f"/experiments/{eid}/manifest",
            json={"manifest": "schema_version: '1.0'", "if_match": 0},
            auth=AUTH,
        )

    elapsed, in_flight, save = _run_with_health_probe(app, fire)
    assert elapsed < 1.5, f"/health took {elapsed:.2f}s behind a validating save"
    assert in_flight, "the save had already finished — the probe raced nothing"
    assert save.status_code == 200
    assert save.json() == {"version": 1}


def test_health_answers_while_cohort_create_rebuilds_indices(tmp_path: Path, monkeypatch) -> None:
    """A cohort create whose index rebuild takes 2.5 s must not freeze the
    loop. The stub returns no matching perms, so the create itself lands 400
    after its slow section — the loop staying free is the capability."""
    _auth_env(monkeypatch)
    _round_with_metadata(tmp_path)
    monkeypatch.setattr(
        appmod, "_matching_permutation_ids", lambda _root, _f, _w: time.sleep(2.5) or {},
    )
    app = create_app(tmp_path)

    async def fire(client: httpx.AsyncClient):
        return await client.post(
            "/cohorts",
            json={
                "name": "c",
                "cohort_type": "direction",
                "rounds": [{"experiment_id": "e1", "round_id": "0001"}],
                "filters": {},
            },
            auth=AUTH,
        )

    elapsed, in_flight, create = _run_with_health_probe(app, fire)
    assert elapsed < 1.5, f"/health took {elapsed:.2f}s behind an indexing cohort create"
    assert in_flight, "the create had already finished — the probe raced nothing"
    assert create.status_code == 400


def test_concurrent_same_name_experiment_creates_get_distinct_ids(
    tmp_path: Path, monkeypatch,
) -> None:
    """Eight threads create 'same' simultaneously; the mkdir claim must hand
    out eight distinct ids, each with a complete experiment dir."""
    _auth_env(monkeypatch)
    app = create_app(tmp_path)
    results: list[tuple[int, str]] = []
    results_lock = threading.Lock()

    def _create() -> None:
        r = TestClient(app).post("/experiments", json={"name": "same"}, auth=AUTH)
        with results_lock:
            results.append((r.status_code, r.json().get("id", "")))

    threads = [threading.Thread(target=_create) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert [status for status, _ in results] == [200] * 8
    ids = [eid for _, eid in results]
    assert len(set(ids)) == 8, f"id collision under concurrency: {sorted(ids)}"
    for eid in ids:
        exp_dir = tmp_path / "experiments" / eid
        assert (exp_dir / "manifest.yml").is_file()
        assert (exp_dir / "rounds").is_dir()
        assert (exp_dir / "version.json").is_file()


def test_concurrent_activations_keep_one_active_per_series(
    tmp_path: Path, monkeypatch,
) -> None:
    """Two cohorts on one arrow series, activated concurrently over and over:
    the flip lock must leave exactly one active every time. Unserialized, both
    flips can read the pre-flip peer state and both finish active."""
    _auth_env(monkeypatch)
    meta = {
        "yaml_reference": {"sfd": {"manifest": {"data_source": {
            "method": "limen.data.HistoricalData.get_arrow_file",
            "params": {"file_path": "/opt/arrow/time_15m/latest.arrow"},
        }}}},
    }
    url_ids: list[str] = []
    for n, perm in (("a", "p1"), ("b", "p2")):
        exp_dir = tmp_path / f"round_{n}"
        exp_dir.mkdir()
        (exp_dir / "metadata.json").write_text(json.dumps(meta))
        cohort_id = cohorts.write_cohort_definition(
            tmp_path, name=n, cohort_type="direction", experiment_dir=str(exp_dir),
            manifest_id=_MANIFEST_ID, permutation_ids=[perm],
        )
        url_ids.append(cohort_id.replace(":", "_"))
    app = create_app(tmp_path)

    def _activate(url_id: str) -> None:
        r = TestClient(app).post(f"/cohorts/{url_id}/active", json={"active": True}, auth=AUTH)
        assert r.status_code == 200

    for _ in range(10):
        threads = [threading.Thread(target=_activate, args=(u,)) for u in url_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        active = [d["name"] for d in cohorts.list_cohort_definitions(tmp_path) if d.get("active")]
        assert len(active) == 1, f"both cohorts active after concurrent flips: {active}"
        for u in url_ids:
            TestClient(app).post(f"/cohorts/{u}/active", json={"active": False}, auth=AUTH)
