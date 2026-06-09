"""Public, machine-readable reference for the Furnace HTTP API."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute

# Paths the auth middleware serves without credentials. Single source of truth
# shared with create_app's middleware, so the documented `auth` flag of every
# endpoint cannot disagree with what the server actually enforces.
_PUBLIC_PATHS = frozenset({"/health", "/api-reference", "/static", "/search/bundle"})


def is_public(method: str, path: str) -> bool:
    if path in _PUBLIC_PATHS or path.startswith("/static/"):
        return True
    return method == "HEAD" and path == "/"


def build_reference(app: FastAPI, *, app_name: str) -> dict[str, object]:
    """Derive the endpoint catalogue from the live FastAPI route table."""
    routes = [route for route in app.routes if isinstance(route, APIRoute)]
    routes.sort(key=lambda route: route.path)
    endpoints: list[dict[str, object]] = []
    for route in routes:
        methods = sorted(method for method in route.methods if method != "HEAD")
        if not methods:
            continue
        public = all(is_public(method, route.path) for method in methods)
        endpoints.append({
            "methods": methods,
            "path": route.path,
            "name": route.name,
            "auth": not public,
        })
    return {
        "app": app_name,
        "auth": {
            "schemes": ["Basic", "Bearer"],
            "public_paths": sorted(_PUBLIC_PATHS | {"/static/*"}),
            "description": "HTTP Basic or Bearer on every endpoint except the "
                           "public paths; 401 with WWW-Authenticate otherwise.",
        },
        "openapi": "/openapi.json",
        "docs": "/docs",
        "endpoints": endpoints,
    }
