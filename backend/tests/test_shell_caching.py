"""The HTML shell must never be served from a stale browser cache after a deploy."""
from fastapi.testclient import TestClient

from app.main import app


def test_shell_is_revalidated_on_every_load():
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
