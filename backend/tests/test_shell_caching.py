"""The HTML shell must never be served from a stale browser cache after a deploy."""
from fastapi.testclient import TestClient

from app.main import app


def test_shell_is_revalidated_on_every_load():
    client = TestClient(app)
    for path in ("/", "/overview"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers.get("cache-control") == "no-cache"
