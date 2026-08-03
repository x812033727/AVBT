"""An empty slug ("/api/tracked/studio//missing-codes") is never a real
listing, but JavBus serves a generic listing page for the empty-slug
URL, so before the guard the scan walked that phantom catalog, treated
every held code (~17k) as belonging to it, and burned up to 50 detail
probes verifying them. The router must 404 without ever reaching the
service, and the service must refuse blank slugs from any other caller
(tracker, aggregates)."""

import httpx
import pytest

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


async def test_empty_slug_missing_codes_404_without_scan(tmp_path, monkeypatch):
    client, engine = await _client(tmp_path, monkeypatch)

    async def boom(*a, **kw):  # pragma: no cover - must not be reached
        raise AssertionError("missing_for_listing must not run for empty slug")

    monkeypatch.setattr(
        tracked.missing_svc, "missing_for_listing", boom, raising=True
    )
    try:
        resp = await client.get("/api/tracked/studio//missing-codes")
        assert resp.status_code == 404
        # Whitespace-only slug is the same phantom listing.
        resp = await client.get("/api/tracked/studio/%20/missing-codes")
        assert resp.status_code == 404
    finally:
        await client.aclose()
        await engine.dispose()


async def test_service_refuses_blank_slug(tmp_path, monkeypatch):
    engine = db.create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True
    )
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = db.async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", maker)
    monkeypatch.setattr(missing_svc, "SessionLocal", maker)

    called = False

    async def fake_fetch(*a, **kw):  # pragma: no cover - must not be reached
        nonlocal called
        called = True
        return ([], 0)

    monkeypatch.setattr(
        missing_svc, "fetch_all_listing_codes", fake_fetch, raising=True
    )
    try:
        for slug in ("", " ", "/"):
            with pytest.raises(ValueError):
                await missing_svc.missing_for_listing("studio", slug)
        assert called is False
    finally:
        await engine.dispose()
