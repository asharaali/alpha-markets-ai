"""The HTML shell must never be served from a stale browser cache after a deploy."""
from fastapi.testclient import TestClient

from app.main import app


def test_shell_is_revalidated_on_every_load(terminal_build):
    client = TestClient(app)
    for path in ("/", "/overview"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers.get("cache-control") == "no-cache"


def test_spa_route_cannot_escape_the_static_directory():
    client = TestClient(app)
    for path in ("/..%2Fapp%2Fconfig.py", "/%2e%2e/app/config.py",
                 "/%2e%2e%2f%2e%2e%2fbackend%2fapp%2fconfig.py",
                 "/..%2F..%2F..%2F..%2F..%2F..%2Fetc%2Fpasswd",
                 "/..%2F..%2F..%2F..%2F..%2F..%2Fproc%2Fself%2Fenviron"):
        response = client.get(path)
        assert "Central configuration" not in response.text
        assert "root:" not in response.text
        assert "SITE_SECRET" not in response.text


# ── terminal at / and /demo, old app at /classic ───────────────────────────────
import pytest

from app import main as main_module


@pytest.fixture
def terminal_build(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<!-- TERMINAL-SHELL -->")
    monkeypatch.setattr(main_module, "TERMINAL_DIR", tmp_path)
    return tmp_path


def test_terminal_served_at_root_demo_and_client_routes(terminal_build):
    client = TestClient(app)
    for path in ("/", "/demo", "/demo/board", "/board", "/macro"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "TERMINAL-SHELL" in response.text, path
        assert response.headers.get("cache-control") == "no-cache", path


def test_classic_app_still_served(terminal_build):
    client = TestClient(app)
    for path in ("/classic", "/classic/overview"):
        response = client.get(path)
        assert response.status_code == 200
        assert "/static/js/app.js" in response.text
        assert response.headers.get("cache-control") == "no-cache"


def test_missing_terminal_build_is_503_with_instructions(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "TERMINAL_DIR", tmp_path / "absent")
    response = TestClient(app).get("/")
    assert response.status_code == 503
    assert "cd frontend && npm run build" in response.text
