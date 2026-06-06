from __future__ import annotations

from pathlib import Path

import pytest

import furnace.app as app_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_SOURCE = (REPO_ROOT / "furnace" / "app.py").read_text(encoding="utf-8")


def test_version_info_reads_build_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_SHA", "abc1234")
    monkeypatch.setenv("BUILD_DATE", "2026-06-06T00:00:00Z")
    monkeypatch.setenv("GIT_SUBJECT", "feat: add /version build-provenance endpoint")
    assert app_mod._version_info() == {
        "sha": "abc1234",
        "date": "2026-06-06T00:00:00Z",
        "subject": "feat: add /version build-provenance endpoint",
    }


def test_version_info_defaults_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("GIT_SHA", "BUILD_DATE", "GIT_SUBJECT"):
        monkeypatch.delenv(key, raising=False)
    assert app_mod._version_info() == {"sha": "", "date": "", "subject": ""}


def test_version_route_delegates_to_version_info() -> None:
    assert '@app.get("/version")' in APP_SOURCE
    assert "return _version_info()" in APP_SOURCE
