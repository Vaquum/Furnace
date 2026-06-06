from __future__ import annotations

import re
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


def test_version_route_registers_version_info() -> None:
    # Registered via add_api_route, not @app.get: that keeps _version_info a
    # referenced argument, so strict pyright does not flag it as an unused
    # decorated route closure (which would exceed the frozen typing budget).
    assert re.search(r"add_api_route\(\s*['\"]/version['\"],\s*_version_info", APP_SOURCE)


def test_version_is_auth_exempt_like_health() -> None:
    """/version is public build provenance (read unauthenticated by Smithy's
    landing tile), so it must sit in the auth middleware's exemption alongside
    /health rather than 401."""
    assert re.search(r"path in \([^)]*['\"]/version['\"]", APP_SOURCE)
