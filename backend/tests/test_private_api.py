"""The terminal is private: every API route needs a session unless it is explicitly allowlisted.

The old gate listed what was PUBLIC by prefix, so research data (slate, strategies, track
record) was readable by anyone with the URL, and any new route under a public prefix became
public silently. Now the allowlist is four exact paths and everything else is private.
"""
import re
import time

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.api.privacy import PUBLIC_API_PATHS
from app.main import app


def _api_get_paths():
    # The OpenAPI table lists every included router's routes, however FastAPI nests them.
    for path, methods in app.openapi()["paths"].items():
        if path.startswith("/api/") and "get" in methods:
            yield re.sub(r"\{[^}]+\}", "x", path)


def test_allowlist_is_exactly_four_paths():
    assert PUBLIC_API_PATHS == frozenset(
        {"/api/health", "/api/login", "/api/logout", "/api/me"})


def test_every_api_route_is_private_unless_allowlisted():
    client = TestClient(app)
    checked = 0
    for path in _api_get_paths():
        if path in PUBLIC_API_PATHS:
            continue
        response = client.get(path)
        assert response.status_code == 401, f"{path} answered {response.status_code} without a session"
        checked += 1
    assert checked >= 15  # the route table really was walked


def test_unknown_api_path_is_private_too():
    assert TestClient(app).get("/api/strategies-v2-not-yet-built").status_code == 401


def test_allowlisted_routes_stay_public():
    client = TestClient(app)
    assert client.get("/api/me").json() == {"user": None}
    assert client.get("/api/health").status_code == 200


@pytest.fixture
def account():
    name = f"priv{int(time.time() * 1000) % 10**9}"
    auth.create_user(name, "terminal-password")
    yield name
    auth.disable_user(name)


def test_logged_in_user_reaches_private_routes(account):
    client = TestClient(app)
    assert client.post("/api/login", json={"username": account,
                                           "password": "terminal-password"}).status_code == 200
    assert client.get("/api/status/jobs").status_code == 200


def test_non_api_paths_are_not_gated():
    assert TestClient(app).get("/").status_code != 401
