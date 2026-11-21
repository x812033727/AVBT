"""A JavBus walk that comes back with zero works (404 / challenge page /
markup change — no exception) must not become the listing's catalog.

It used to be persisted and then served "at any age", so a listing stayed
at 共 0 部 with every card marked 缺漏 until someone forced a re-walk.
Listing pages also get 已下載 from held codes directly (PikPak ∪ Drive),
independent of the catalog."""

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.database as db
import app.routers.tracked as tracked_router
import app.services.listing_catalog as lcat
from app.models import GDriveLocation, ListingCatalog
from app.services import missing as missing_svc
from app.services.missing import MovieListItem


async def _bind(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/c.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(missing_svc, "SessionLocal", maker)
    monkeypatch.setattr(lcat, "SessionLocal", maker)
    monkeypatch.setattr(missing_svc, "_listing_cache", {})
    return engine, maker


def _walk_returns(monkeypatch, codes):
    calls = []

    async def fake_walk(kind, slug, *, uncensored, max_pages, with_magnets_only=True, **kw):
        calls.append(slug)
        return [MovieListItem(code=c, title=c) for c in codes], 1

    monkeypatch.setattr(missing_svc, "walk_listing", fake_walk)
    return calls


async def _put(maker, codes, flt="mag"):
    async with maker() as s:
        s.add(ListingCatalog(
            kind="studio", id="3pu", filter=flt, uncensored=False,
            items=json.dumps([{"code": c, "title": c} for c in codes]),
            pages_scanned=1, fetched_at=datetime.utcnow(),
        ))
        await s.commit()


async def _stored(maker):
    async with maker() as s:
        row = (await s.execute(select(ListingCatalog))).scalars().first()
    return json.loads(row.items) if row else None


async def test_empty_walk_keeps_good_catalog(tmp_path, monkeypatch):
    engine, maker = await _bind(tmp_path, monkeypatch)
    await _put(maker, ["KBTK-019", "KBTK-020"])
    _walk_returns(monkeypatch, [])
    items, _ = await missing_svc.fetch_all_listing_codes("studio", "3pu", uncensored=False, refresh=True)
    assert [i.code for i in items] == ["KBTK-019", "KBTK-020"]
    assert len(await _stored(maker)) == 2  # not wiped
    await engine.dispose()


async def test_empty_walk_without_catalog_is_not_persisted(tmp_path, monkeypatch):
    engine, maker = await _bind(tmp_path, monkeypatch)
    _walk_returns(monkeypatch, [])
    items, _ = await missing_svc.fetch_all_listing_codes("studio", "3pu", uncensored=False)
    assert items == []
    assert await _stored(maker) is None
    await engine.dispose()


async def test_persisted_empty_catalog_is_rewalked(tmp_path, monkeypatch):
    engine, maker = await _bind(tmp_path, monkeypatch)
    await _put(maker, [])  # the stuck row from before this fix
    calls = _walk_returns(monkeypatch, ["KBTK-020"])
    items, _ = await missing_svc.fetch_all_listing_codes("studio", "3pu", uncensored=False)
    assert [i.code for i in items] == ["KBTK-020"] and calls == ["3pu"]
    assert [i["code"] for i in await _stored(maker)] == ["KBTK-020"]
    await engine.dispose()


async def test_nonempty_walk_persists(tmp_path, monkeypatch):
    engine, maker = await _bind(tmp_path, monkeypatch)
    _walk_returns(monkeypatch, ["KBTK-001"])
    await missing_svc.fetch_all_listing_codes("studio", "3pu", uncensored=False)
    assert [i["code"] for i in await _stored(maker)] == ["KBTK-001"]
    await engine.dispose()


async def test_held_endpoint(tmp_path, monkeypatch):
    engine, maker = await _bind(tmp_path, monkeypatch)

    async def fake_presence(*, force=False):
        return {"KBTK-018"}

    monkeypatch.setattr(missing_svc.presence_index, "get", fake_presence)
    async with maker() as s:
        s.add(GDriveLocation(code="KBTK-020", account_id=1, drive_file_id="v", path="a", is_video=True))
        s.add(GDriveLocation(code="KBTK-017", account_id=1, drive_file_id="c", path="b", is_video=False))
        await s.commit()
    out = await tracked_router.held_codes(["KBTK-017", "KBTK-018", "KBTK-019", "KBTK-020"])
    assert out == {"codes": ["KBTK-018", "KBTK-020"]}
    await engine.dispose()


async def test_missing_codes_maps_walk_failure_to_502(tmp_path, monkeypatch):
    import httpx
    import pytest
    from fastapi import HTTPException

    engine, maker = await _bind(tmp_path, monkeypatch)

    async def failing(*a, **kw):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(missing_svc, "missing_for_listing", failing)
    async with maker() as s:
        with pytest.raises(HTTPException) as ei:
            await tracked_router.missing_codes_for("studio", "3pu", session=s)
    assert ei.value.status_code == 502 and "JavBus" in ei.value.detail
    await engine.dispose()
