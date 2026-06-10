"""The public /api-reference catalogue matches the live routes and auth."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from furnace import api_reference
from furnace.app import create_app


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setenv("APP_USERNAME", "u")
    monkeypatch.setenv("APP_PASSWORD", "p")
    monkeypatch.setenv("API_SECRET", "s")
    (tmp_path / "experiments").mkdir()
    return create_app(tmp_path)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _api_route_paths(app: FastAPI) -> set[str]:
    return {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
        and any(method != "HEAD" for method in route.methods)
    }


def test_api_reference_is_public(client: TestClient) -> None:
    resp = client.get("/api-reference")
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["app"] == "Furnace"
    assert doc["openapi"] == "/openapi.json"


def test_reference_lists_exactly_the_api_routes(
    app: FastAPI, client: TestClient
) -> None:
    doc = client.get("/api-reference").json()
    documented = {entry["path"] for entry in doc["endpoints"]}
    assert documented == _api_route_paths(app)


def test_auth_flags_match_is_public(client: TestClient) -> None:
    doc = client.get("/api-reference").json()
    by_path = {entry["path"]: entry for entry in doc["endpoints"]}
    assert by_path["/health"]["auth"] is False
    assert by_path["/api-reference"]["auth"] is False
    assert by_path["/search/bundle"]["auth"] is False
    assert by_path["/version"]["auth"] is True
    assert by_path["/"]["auth"] is True


def test_health_and_reference_public_but_version_authed(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
    assert client.get("/api-reference").status_code == 200
    assert client.get("/version").status_code == 401


def test_is_public_predicate() -> None:
    assert api_reference.is_public("GET", "/health")
    assert api_reference.is_public("GET", "/api-reference")
    assert api_reference.is_public("GET", "/search/bundle")
    assert api_reference.is_public("GET", "/static/app.css")
    assert api_reference.is_public("HEAD", "/")
    assert not api_reference.is_public("GET", "/")
    assert not api_reference.is_public("GET", "/version")
