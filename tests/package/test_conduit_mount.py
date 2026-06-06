"""The Furnace deploy mounts the Conduit prediction store read-write at
/opt/conduit — the producer-side mirror of the read-only /opt/arrow mount.

Source-level checks on deploy/docker-compose.yml — the codebase's test idiom
(the app cannot be imported without its full runtime, so deploy invariants are
asserted against the compose file rather than a running container). These pin
the contract that lets Furnace write per-series prediction Arrow files to
/opt/conduit/<series>/latest.arrow for same-host consumers to read zero-copy.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "deploy" / "docker-compose.yml"


def _furnace_mounts() -> list[object]:
    spec = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return spec["services"]["furnace"]["volumes"]


def test_conduit_mount_present_and_rw() -> None:
    """Exactly one furnace mount targets /opt/conduit, and it is a read-WRITE
    volume mount — Furnace produces the prediction files there, so unlike the
    read-only /opt/arrow mount this one must allow writes (read_only not True)."""
    conduit = [
        m for m in _furnace_mounts()
        if isinstance(m, dict) and m.get("target") == "/opt/conduit"
    ]
    assert len(conduit) == 1, conduit
    mount = conduit[0]
    assert mount["type"] == "volume"
    assert mount.get("read_only") is not True
    assert mount["source"] == "conduit"


def test_conduit_mount_shape() -> None:
    """The top-level `conduit` volume is external, so this compose project never
    creates or removes it — a teardown never deletes served predictions. Its
    name is parameterised and defaults to a Furnace-owned volume."""
    spec = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    conduit = spec["volumes"]["conduit"]
    assert conduit["external"] is True
    assert "conduit" in str(conduit.get("name", ""))


def test_arrow_mount_unchanged() -> None:
    """The /opt/arrow mount from #59 stays a read-only external volume — the
    conduit mount is added alongside it, never in place of it (no regression)."""
    arrow = [
        m for m in _furnace_mounts()
        if isinstance(m, dict) and m.get("target") == "/opt/arrow"
    ]
    assert len(arrow) == 1, arrow
    assert arrow[0]["type"] == "volume"
    assert arrow[0]["read_only"] is True
    assert arrow[0]["source"] == "arrow"


def test_data_experiments_remains_first_rw_bind() -> None:
    """The /data/experiments bind stays the first mount and stays writable —
    every new mount is added after it, not in place of it."""
    first = _furnace_mounts()[0]
    assert first["type"] == "bind"
    assert first["target"] == "/data/experiments"
    assert first.get("read_only") is not True
