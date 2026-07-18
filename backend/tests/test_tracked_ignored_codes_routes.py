"""Route-registration smoke test for the /api/tracked/ignored-codes
endpoints.

tracked.py ends with a catch-all ``/{kind}/{slug:path}`` GET and
``DELETE`` pair used for untrack. A two-segment path like
``DELETE /ignored-codes/ABC-002`` would be swallowed by that catch-all
as ``(kind="ignored-codes", slug="ABC-002")`` — a 404 "not tracked" that
looks superficially fine — unless the ignored-codes routes are declared
above it. Service-level tests (test_missing_ignored_codes.py) exercise
the underlying functions directly and can't catch a routing-order bug
like that; only an actual ASGI dispatch can."""

import httpx

import app.database as db
from app.routers import tracked
from app.services import missing as missing_svc


async def _client(tmp_path, monkeypatch):
    from fastapi import FastAPI

    engine = db.create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True
    )
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = db.async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", maker)
    monkeypatch.setattr(missing_svc, "SessionLocal", maker)

    app = FastAPI()
    app.include_router(tracked.router)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return client, engine


async def test_delete_ignored_code_hits_ignore_handler_not_untrack_catchall(
    tmp_path, monkeypatch
):
    client, engine = await _client(tmp_path, monkeypatch)
    try:
        add = await client.post(
            "/api/tracked/ignored-codes",
            json={"code": "abc-002", "reason": "no magnet"},
        )
        assert add.status_code == 200
        assert add.json() == {"ABC-002": "no magnet"}

        listed = await client.get("/api/tracked/ignored-codes")
        assert listed.status_code == 200
        assert listed.json() == {"ABC-002": "no magnet"}

        # If this fell through to the untrack catch-all it would 404
        # with "not tracked" (no TrackedListing row named "ignored-codes"
        # exists) instead of actually removing the ignore entry.
        deleted = await client.delete("/api/tracked/ignored-codes/abc-002")
        assert deleted.status_code == 200
        assert deleted.json() == {}

        after = await client.get("/api/tracked/ignored-codes")
        assert after.json() == {}
    finally:
        await client.aclose()
        await engine.dispose()
