"""Deploy contract for the prediction-refresh scheduler service.

The scheduler is a separate ``python -m furnace --scheduler`` process beside
the web app. These checks pin the Compose surface that keeps live Conduit
prediction files advancing without coupling the loop to the HTTP server.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "deploy" / "docker-compose.yml"


def _services():
    spec = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return spec["services"]


def _scheduler_service():
    service = _services()["furnace-scheduler"]
    assert isinstance(service, dict)
    return service


def _mounts_by_target():
    return {
        mount.get("target"): mount
        for mount in _scheduler_service()["volumes"]
        if isinstance(mount, dict)
    }


def test_scheduler_service_runs_scheduler_flag() -> None:
    schedulers = [
        name
        for name, service in _services().items()
        if isinstance(service, dict) and "--scheduler" in str(service.get("command"))
    ]

    assert schedulers == ["furnace-scheduler"]
    assert _scheduler_service()["image"] == "${FURNACE_IMAGE:?FURNACE_IMAGE must be set}"
    assert _scheduler_service()["init"] is True
    assert _scheduler_service()["command"] == [
        "python",
        "-m",
        "furnace",
        "--root",
        "/data/experiments",
        "--scheduler",
    ]


def test_scheduler_service_has_serving_mounts() -> None:
    mounts = _mounts_by_target()

    data = mounts["/data/experiments"]
    assert data["type"] == "bind"
    assert data["source"] == "${FURNACE_HOST_DATA_ROOT:-/data/experiments}"
    assert data["read_only"] is False

    arrow = mounts["/opt/arrow"]
    assert arrow["type"] == "volume"
    assert arrow["source"] == "arrow"
    assert arrow["read_only"] is True

    conduit = mounts["/opt/conduit"]
    assert conduit["type"] == "volume"
    assert conduit["source"] == "conduit"
    assert conduit["read_only"] is False


def test_scheduler_service_caps_blas_threads() -> None:
    """Cold sensor reconstruction must match run_round's numeric threading
    contract. Without these caps, sklearn/OpenBLAS can fan one scheduler fit
    across the whole host and change reconstruction metrics."""
    assert _scheduler_service()["environment"] == {
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }


def test_scheduler_service_restart_policy() -> None:
    assert _scheduler_service()["restart"] == "unless-stopped"


def test_web_service_has_no_scheduler_flag() -> None:
    web = _services()["furnace"]
    assert isinstance(web, dict)
    assert "--scheduler" not in str(web.get("command"))
