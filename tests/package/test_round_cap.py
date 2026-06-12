"""Tests for S93 (PRD #137): a global cap on concurrently active rounds.

The per-experiment gate already refuses a second round in the SAME
experiment; these tests pin the cross-experiment cap: at the default
(`FURNACE_MAX_ACTIVE_ROUNDS=1`) a start anywhere answers 409 naming the
blocking round while any round is active, a raised cap admits exactly that
many, and a garbage cap refuses startup. Fake active rounds are written
AFTER create_app so the startup reconciler does not finalize them.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import furnace.app as appmod
from furnace.app import create_app

AUTH = ("u", "p")


def _auth_env(monkeypatch) -> None:
    monkeypatch.setenv("APP_USERNAME", "u")
    monkeypatch.setenv("APP_PASSWORD", "p")
    monkeypatch.setenv("API_SECRET", "s")


class _ParkedPopen:
    """A spawn stub whose round never exits — the started round stays
    `pending` (active) without running a real `limen run` (same stub as
    test_delete_locking)."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.pid = os.getpid()

    def wait(self) -> int:
        threading.Event().wait()
        return 0

    def poll(self) -> None:
        return None


def _fake_active_round(root: Path, eid: str, rid: str = "0001") -> None:
    """An on-disk round in state `running` — written after create_app, so it
    exists only for the request-time cap scan."""
    round_dir = root / "experiments" / eid / "rounds" / rid
    round_dir.mkdir(parents=True)
    (round_dir / "status.json").write_text(json.dumps({
        "state": "running", "pid": 4_194_304,
        "started_at": "2026-06-12T00:00:00Z", "ended_at": None, "exit_code": None,
    }))


def test_default_cap_blocks_second_round_across_experiments(
    tmp_path: Path, monkeypatch,
) -> None:
    """With the default cap (1) and an active round in experiment A, a start
    in experiment B answers 409 — the gate the per-experiment check never
    covered."""
    _auth_env(monkeypatch)
    monkeypatch.setattr(appmod, "_manifest_errors", lambda _m: [])
    app = create_app(tmp_path)
    client = TestClient(app)
    eid_a = client.post("/experiments", json={"name": "busy"}, auth=AUTH).json()["id"]
    eid_b = client.post("/experiments", json={"name": "idle"}, auth=AUTH).json()["id"]
    _fake_active_round(tmp_path, eid_a)

    r = client.post(f"/experiments/{eid_b}/rounds", json={"if_match": 0}, auth=AUTH)
    assert r.status_code == 409
    assert "active-round cap (1) reached" in r.json()["error"]


def test_cap_409_names_the_blocking_round(tmp_path: Path, monkeypatch) -> None:
    """The 409 body's `error` carries the blocking `<experiment>/<round>` —
    the string the start modal renders, so the operator knows what to
    terminate or wait for."""
    _auth_env(monkeypatch)
    monkeypatch.setattr(appmod, "_manifest_errors", lambda _m: [])
    app = create_app(tmp_path)
    client = TestClient(app)
    eid_a = client.post("/experiments", json={"name": "busy"}, auth=AUTH).json()["id"]
    eid_b = client.post("/experiments", json={"name": "idle"}, auth=AUTH).json()["id"]
    _fake_active_round(tmp_path, eid_a, rid="0003")

    r = client.post(f"/experiments/{eid_b}/rounds", json={"if_match": 0}, auth=AUTH)
    assert r.status_code == 409
    assert f"{eid_a}/0003" in r.json()["error"]
    assert "FURNACE_MAX_ACTIVE_ROUNDS" in r.json()["error"]


def test_raised_cap_allows_concurrent_rounds(tmp_path: Path, monkeypatch) -> None:
    """FURNACE_MAX_ACTIVE_ROUNDS=2 admits a second concurrent round and
    refuses the third — the cap counts rounds, not experiments."""
    _auth_env(monkeypatch)
    monkeypatch.setenv("FURNACE_MAX_ACTIVE_ROUNDS", "2")
    monkeypatch.setattr("subprocess.Popen", _ParkedPopen)
    monkeypatch.setattr(appmod, "_manifest_errors", lambda _m: [])
    app = create_app(tmp_path)
    client = TestClient(app)
    eid_a = client.post("/experiments", json={"name": "busy"}, auth=AUTH).json()["id"]
    eid_b = client.post("/experiments", json={"name": "second"}, auth=AUTH).json()["id"]
    eid_c = client.post("/experiments", json={"name": "third"}, auth=AUTH).json()["id"]
    _fake_active_round(tmp_path, eid_a)

    second = client.post(f"/experiments/{eid_b}/rounds", json={"if_match": 0}, auth=AUTH)
    assert second.status_code == 200

    third = client.post(f"/experiments/{eid_c}/rounds", json={"if_match": 0}, auth=AUTH)
    assert third.status_code == 409
    assert "active-round cap (2) reached" in third.json()["error"]


def test_invalid_cap_refuses_startup(tmp_path: Path, monkeypatch) -> None:
    """A cap that is not an integer >= 1 refuses startup with the variable
    named — the same fail-loud posture as the auth vars."""
    _auth_env(monkeypatch)
    monkeypatch.setenv("FURNACE_MAX_ACTIVE_ROUNDS", "zebra")
    with pytest.raises(RuntimeError, match="FURNACE_MAX_ACTIVE_ROUNDS"):
        create_app(tmp_path)
    monkeypatch.setenv("FURNACE_MAX_ACTIVE_ROUNDS", "0")
    with pytest.raises(RuntimeError, match="FURNACE_MAX_ACTIVE_ROUNDS"):
        create_app(tmp_path)
