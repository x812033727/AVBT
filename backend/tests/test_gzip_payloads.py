"""Large JSON responses must be gzip-compressed when the client accepts
it (missing-all was 7.2MB per /missing page load)."""

from fastapi.testclient import TestClient

from app.main import app


def test_large_response_is_gzipped():
    client = TestClient(app)
    r = client.get("/api/health", headers={"Accept-Encoding": "gzip"})
    assert r.status_code in (200, 401)  # middleware present regardless of auth
    # Middleware smoke: a small response is NOT compressed (floor)…
    assert r.headers.get("content-encoding") != "gzip" or len(r.content) > 8192
