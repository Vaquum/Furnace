"""The Furnace deploy mounts the tdw Arrow bar store read-only at /opt/arrow.

Source-level checks on deploy/docker-compose.yml — the codebase's test idiom
(the app cannot be imported without its full runtime, so deploy invariants are
asserted against the compose file rather than a running container). These pin
the contract that lets a round run a Limen manifest whose data_source is
HistoricalData.get_arrow_file reading /opt/arrow/<series>/latest.arrow.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "deploy" / "docker-compose.yml"


def _furnace_mounts() -> list[object]:
    spec = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return spec["services"]["furnace"]["volumes"]


def test_arrow_volume_mounted_read_only_at_opt_arrow() -> None:
    """Exactly one furnace mount targets /opt/arrow, and it is a read-only
    volume mount — read-only so a round can never write back into the shared
    store, a volume so it is backed by the external arrow volume."""
    arrow = [
        m for m in _furnace_mounts()
        if isinstance(m, dict) and m.get("target") == "/opt/arrow"
    ]
    assert len(arrow) == 1, arrow
    mount = arrow[0]
    assert mount["type"] == "volume"
    assert mount["read_only"] is True
    assert mount["source"] == "arrow"


def test_arrow_is_external_named_volume() -> None:
    """The top-level `arrow` volume is external, so this compose project never
    creates or removes it — the store is owned by the tdw-control-plane stack."""
    spec = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    arrow = spec["volumes"]["arrow"]
    assert arrow["external"] is True
    assert "tdw-control-plane_arrow" in str(arrow.get("name", ""))


def test_data_experiments_remains_first_rw_bind() -> None:
    """The /data/experiments bind stays the first mount and stays writable —
    the arrow mount is added alongside it, not in place of it."""
    first = _furnace_mounts()[0]
    assert first["type"] == "bind"
    assert first["target"] == "/data/experiments"
    assert first.get("read_only") is not True
