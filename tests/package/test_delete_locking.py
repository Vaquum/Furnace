"""Tests for S92 (PRD #137): deletion takes the experiment lock and
disappears atomically.

The headline test reproduces the orphaned-run race deterministically: a round
start holds the experiment lock through a slow (stubbed) validate while a
delete arrives. Unlocked, the delete's active-check passes before the round
exists and its rmtree pulls the experiment out from under the spawn — both
requests succeed. Locked, the delete waits and answers 409. The spawn is a
parked stub, so a "running" round stays `pending` (active) without ever
executing a real `limen run`.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

import furnace.app as appmod
from furnace.app import create_app

AUTH = ("u", "p")


def _auth_env(monkeypatch) -> None:
    monkeypatch.setenv("APP_USERNAME", "u")
    monkeypatch.setenv("APP_PASSWORD", "p")
    monkeypatch.setenv("API_SECRET", "s")


class _ParkedPopen:
    """A spawn stub whose round never exits: ``pid`` is the test process (so
    the watcher's process-group probe stays parked) and ``wait()`` blocks for
    the daemon thread's lifetime — the started round stays ``pending``, i.e.
    ACTIVE, without running limen."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.pid = os.getpid()

    def wait(self) -> int:
        threading.Event().wait()
        return 0

    def poll(self) -> None:
        return None


def test_delete_and_start_cannot_both_succeed(tmp_path: Path, monkeypatch) -> None:
    """A delete arriving while a start holds the experiment lock (inside its
    validate) must wait and then refuse: the start wins, the round dir
    survives, the delete is 409. Unlocked, both succeed — the experiment is
    rmtree'd under the just-spawned round."""
    _auth_env(monkeypatch)
    monkeypatch.setattr("subprocess.Popen", _ParkedPopen)
    monkeypatch.setattr(appmod, "_manifest_errors", lambda _m: time.sleep(1.2) or [])
    app = create_app(tmp_path)
    eid = TestClient(app).post("/experiments", json={"name": "racer"}, auth=AUTH).json()["id"]

    results: dict[str, object] = {}

    def _start() -> None:
        results["start"] = TestClient(app).post(
            f"/experiments/{eid}/rounds", json={"if_match": 0}, auth=AUTH,
        )

    t = threading.Thread(target=_start)
    t.start()
    time.sleep(0.4)
    delete = TestClient(app).delete(f"/experiments/{eid}", auth=AUTH)
    t.join()
    start = results["start"]

    assert not (start.status_code == 200 and delete.status_code == 200), (
        "delete and start both succeeded — the orphaned-run race is open"
    )
    assert start.status_code == 200
    assert delete.status_code == 409
    round_dir = tmp_path / "experiments" / eid / "rounds" / start.json()["round_id"]
    assert round_dir.is_dir(), "the started round's dir must survive the refused delete"


def test_start_round_races_a_delete_to_404_not_500(tmp_path: Path, monkeypatch) -> None:
    """When a delete wins the race — the experiment vanishes between the
    handler's resolve and its lock — the start answers 404, never a
    FileNotFoundError 500 out of the version flock."""
    _auth_env(monkeypatch)
    monkeypatch.setattr(appmod, "_manifest_errors", lambda _m: [])
    app = create_app(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    eid = client.post("/experiments", json={"name": "vanish"}, auth=AUTH).json()["id"]

    real_exp_lock = appmod.StateMachine.exp_lock

    def _vanish_then_lock(self, experiment_id: str):
        target = tmp_path / "experiments" / experiment_id
        if target.exists():
            shutil.rmtree(target)
        return real_exp_lock(self, experiment_id)

    monkeypatch.setattr(appmod.StateMachine, "exp_lock", _vanish_then_lock)
    r = client.post(f"/experiments/{eid}/rounds", json={"if_match": 0}, auth=AUTH)
    assert r.status_code == 404


def test_delete_renames_to_trash_before_removal(tmp_path: Path, monkeypatch) -> None:
    """The directory leaves experiments/ by one atomic rename into
    <root>/.trash/; the slow rmtree runs on the tombstone. With rmtree
    stubbed out, the data sits whole in the trash — proving observers never
    see a half-removed experiment."""
    _auth_env(monkeypatch)
    app = create_app(tmp_path)
    client = TestClient(app)
    eid = client.post("/experiments", json={"name": "doomed"}, auth=AUTH).json()["id"]

    removed: list[Path] = []
    monkeypatch.setattr(appmod.shutil, "rmtree", lambda p: removed.append(Path(p)))
    r = client.delete(f"/experiments/{eid}", auth=AUTH)

    assert r.status_code == 200
    assert not (tmp_path / "experiments" / eid).exists()
    assert len(removed) == 1
    tombstone = removed[0]
    assert tombstone.parent == tmp_path / ".trash"
    assert tombstone.name.startswith(eid)
    assert (tombstone / "manifest.yml").is_file()


def test_round_delete_waits_for_the_experiment_lock(tmp_path: Path, monkeypatch) -> None:
    """delete_round serializes on the same lock as start_round: sent while a
    start is inside its validate, it returns only after the start completes —
    so the id it frees can never collide with the one being allocated."""
    _auth_env(monkeypatch)
    monkeypatch.setattr("subprocess.Popen", _ParkedPopen)
    monkeypatch.setattr(appmod, "_manifest_errors", lambda _m: time.sleep(1.2) or [])
    app = create_app(tmp_path)
    client = TestClient(app)
    eid = client.post("/experiments", json={"name": "queue"}, auth=AUTH).json()["id"]
    old_round = tmp_path / "experiments" / eid / "rounds" / "0001"
    old_round.mkdir(parents=True)
    (old_round / "status.json").write_text(json.dumps({
        "state": "terminated", "pid": None,
        "started_at": None, "ended_at": None, "exit_code": None,
    }))

    results: dict[str, object] = {}

    def _start() -> None:
        results["start"] = TestClient(app).post(
            f"/experiments/{eid}/rounds", json={"if_match": 0}, auth=AUTH,
        )

    t = threading.Thread(target=_start)
    t.start()
    time.sleep(0.4)
    t0 = time.monotonic()
    delete = TestClient(app).delete(f"/experiments/{eid}/rounds/0001", auth=AUTH)
    waited = time.monotonic() - t0
    t.join()

    assert delete.status_code == 200
    assert results["start"].status_code == 200
    assert results["start"].json()["round_id"] == "0002", (
        "the new round must be allocated with 0001 still claimed"
    )
    assert waited > 0.5, f"round delete returned in {waited:.2f}s — it did not wait for the lock"
    assert not old_round.exists()


def test_delete_tolerates_max_length_experiment_ids(tmp_path: Path, monkeypatch) -> None:
    """A 250-char experiment id is creatable (under the 255-byte filename
    limit) but its tombstone would not be without the stem cap — uncapped,
    the suffix pushes the rename over the limit (ENAMETOOLONG) and a dir
    that was valid at creation time becomes undeletable."""
    _auth_env(monkeypatch)
    app = create_app(tmp_path)
    client = TestClient(app)
    eid = client.post("/experiments", json={"name": "a" * 250}, auth=AUTH).json()["id"]
    assert len(eid) == 250
    r = client.delete(f"/experiments/{eid}", auth=AUTH)
    assert r.status_code == 200
    assert not (tmp_path / "experiments" / eid).exists()


def test_trash_is_swept_at_startup(tmp_path: Path, monkeypatch) -> None:
    """A tombstone a crash left mid-rmtree is an acknowledged delete; startup
    removes the whole trash dir rather than retaining its data silently."""
    _auth_env(monkeypatch)
    leftover = tmp_path / ".trash" / "old-experiment-ab12cd34"
    leftover.mkdir(parents=True)
    (leftover / "stale.txt").write_text("crash left me here")
    create_app(tmp_path)
    assert not (tmp_path / ".trash").exists()
