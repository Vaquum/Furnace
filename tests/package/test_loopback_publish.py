"""Furnace publishes on the host loopback only.

Portal (host network) is the single entry point to Vaquum services and reaches
Furnace at http://127.0.0.1:8502; a publish on any other host IP makes Furnace
reachable off-host around it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "deploy" / "docker-compose.yml"


def test_furnace_publishes_on_loopback_only() -> None:
    spec = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert spec["services"]["furnace"]["ports"] == ["127.0.0.1:${FURNACE_PORT:-8502}:8502"]
