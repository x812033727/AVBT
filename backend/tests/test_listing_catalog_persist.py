"""Persisted JavBus listing catalogs (``listing_catalog`` table).

The catalog walk results used to live only in missing.py's in-memory
``_listing_cache``, so every restart / hourly invalidation forced a full
JavBus re-crawl of all tracked listings. These tests pin the new
contract: walks write through to the DB, reads fall back to it at any
age, a failed walk never clobbers a good row, and freshness decisions
(``catalog_is_current``) honour the skip-walk window.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as db
from app.models import ListingCatalog
from app.schemas import MovieListItem
from app.services import listing_catalog
from app.services import missing as missing_svc


async def _bind_tmp_db(tmp_path, monkeypatch, name="c.db"):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/{name}", future=True
    )
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", maker)
    # Both modules bind SessionLocal at import time.
    monkeypatch.setattr(missing_svc, "SessionLocal", maker)
    monkeypatch.setattr(listing_catalog, "SessionLocal", maker)
    # Isolate the module-level L1 cache and walk locks per test.
    monkeypatch.setattr(missing_svc, "_listing_cache", {})
    monkeypatch.setattr(missing_svc, "_walk_locks", {})
    return maker


def _item(code: str) -> MovieListItem:
    return MovieListItem(
        code=code, title=f"title {code}",
        cover=f"http://img/{code}.jpg", date="2024-01-01",
    )


async def test_put_get_roundtrip_and_misses(tmp_path, monkeypatch):
    maker = await _bind_tmp_db(tmp_path, monkeypatch)
    items = [_item("ABC-001"), _item("ABC-002")]
    await listing_catalog.put(
        "star", "abc", uncensored=False, with_magnets_only=True,
        items=items, pages_scanned=3,
    )

    got = await listing_catalog.get("star", "abc", uncensored=False)
    assert got is not None
    got_items, pages, fetched_at = got
    assert [i.code for i in got_items] == ["ABC-001", "ABC-002"]
    assert got_items[0].cover == "http://img/ABC-001.jpg"
    assert got_items[0].date == "2024-01-01"
    assert pages == 3
    assert isinstance(fetched_at, datetime)

    # uncensored mismatch = miss (row describes the other variant).
    assert await listing_catalog.get("star", "abc", uncensored=True) is None
    # Different filter axis = independent row.
    assert (
        await listing_catalog.get(
            "star", "abc", uncensored=False, with_magnets_only=False
        )
        is None
    )
    # Unknown listing = miss.
    assert await listing_catalog.get("star", "zzz", uncensored=False) is None

    # Corrupt JSON = miss, never a raise.
    async with maker() as s:
        row = await s.get(ListingCatalog, ("star", "abc", "mag"))
        row.items = "{not json"
        await s.commit()
    assert await listing_catalog.get("star", "abc", uncensored=False) is None


async def test_restart_warm_serves_db_without_walk(tmp_path, monkeypatch):
    await _bind_tmp_db(tmp_path, monkeypatch)
    await listing_catalog.put(
        "star", "abc", uncensored=False, with_magnets_only=True,
        items=[_item("ABC-001")], pages_scanned=1,
    )

    async def boom(*a, **kw):
        raise AssertionError("walked JavBus on a persisted catalog")

    monkeypatch.setattr(missing_svc, "walk_listing", boom)

    # L1 is empty (fresh dict = simulated restart) — must come from DB.
    items, pages = await missing_svc.fetch_all_listing_codes(
        "star", "abc", uncensored=False
    )
    assert [i.code for i in items] == ["ABC-001"]
    assert pages == 1

    # Second call hits the freshly-populated L1 — still no walk.
    items2, _ = await missing_svc.fetch_all_listing_codes(
        "star", "abc", uncensored=False
    )
    assert [i.code for i in items2] == ["ABC-001"]


async def test_failed_walk_preserves_persisted_catalog(tmp_path, monkeypatch):
    await _bind_tmp_db(tmp_path, monkeypatch)
    await listing_catalog.put(
        "star", "abc", uncensored=False, with_magnets_only=True,
        items=[_item("ABC-001")], pages_scanned=1,
    )

    async def boom(*a, **kw):
        raise RuntimeError("javbus 429")

    monkeypatch.setattr(missing_svc, "walk_listing", boom)

    with pytest.raises(RuntimeError):
        await missing_svc.fetch_all_listing_codes(
            "star", "abc", uncensored=False, refresh=True
        )

    got = await listing_catalog.get("star", "abc", uncensored=False)
    assert got is not None and [i.code for i in got[0]] == ["ABC-001"]


async def test_refresh_walk_writes_through(tmp_path, monkeypatch):
    await _bind_tmp_db(tmp_path, monkeypatch)
    await listing_catalog.put(
        "star", "abc", uncensored=False, with_magnets_only=True,
        items=[_item("ABC-001")], pages_scanned=1,
    )
    old = await listing_catalog.get("star", "abc", uncensored=False)

    async def walk(*a, **kw):
        return [_item("ABC-001"), _item("ABC-002")], 2

    monkeypatch.setattr(missing_svc, "walk_listing", walk)

    items, pages = await missing_svc.fetch_all_listing_codes(
        "star", "abc", uncensored=False, refresh=True
    )
    assert len(items) == 2 and pages == 2

    got = await listing_catalog.get("star", "abc", uncensored=False)
    assert got is not None
    assert [i.code for i in got[0]] == ["ABC-001", "ABC-002"]
    assert got[2] >= old[2]


async def test_capped_walk_never_overwrites_full_catalog(tmp_path, monkeypatch):
    await _bind_tmp_db(tmp_path, monkeypatch)
    await listing_catalog.put(
        "star", "abc", uncensored=False, with_magnets_only=True,
        items=[_item("ABC-001"), _item("ABC-002")], pages_scanned=2,
    )

    async def walk(*a, **kw):
        return [_item("ABC-001")], 1

    monkeypatch.setattr(missing_svc, "walk_listing", walk)

    await missing_svc.fetch_all_listing_codes(
        "star", "abc", uncensored=False, refresh=True, max_pages=1
    )
    got = await listing_catalog.get("star", "abc", uncensored=False)
    assert got is not None and len(got[0]) == 2  # full row untouched


async def test_catalog_is_current(tmp_path, monkeypatch):
    maker = await _bind_tmp_db(tmp_path, monkeypatch)

    # No row yet.
    assert not await missing_svc.catalog_is_current(
        "star", "abc", uncensored=False, newest_code="ABC-002"
    )

    await listing_catalog.put(
        "star", "abc", uncensored=False, with_magnets_only=True,
        items=[_item("ABC-001"), _item("ABC-002")], pages_scanned=1,
    )

    # Fresh + newest code present → current.
    assert await missing_svc.catalog_is_current(
        "star", "abc", uncensored=False, newest_code="ABC-002"
    )
    # Newest code absent (new release since the walk) → stale.
    assert not await missing_svc.catalog_is_current(
        "star", "abc", uncensored=False, newest_code="ABC-003"
    )
    # Empty newest code (page-1 failed / empty) → walk.
    assert not await missing_svc.catalog_is_current(
        "star", "abc", uncensored=False, newest_code=""
    )
    # uncensored mismatch → stale.
    assert not await missing_svc.catalog_is_current(
        "star", "abc", uncensored=True, newest_code="ABC-002"
    )

    # Older than the skip-walk window → stale even with the code present.
    async with maker() as s:
        row = await s.get(ListingCatalog, ("star", "abc", "mag"))
        row.fetched_at = datetime.utcnow() - timedelta(hours=13)
        await s.commit()
    assert not await missing_svc.catalog_is_current(
        "star", "abc", uncensored=False, newest_code="ABC-002"
    )


async def test_invalidate_listing_targets_one_listing(tmp_path, monkeypatch):
    await _bind_tmp_db(tmp_path, monkeypatch)
    now = datetime.utcnow()
    cache = missing_svc._listing_cache
    cache[("star", "abc", False, True)] = (now, [_item("ABC-001")], 1)
    cache[("star", "abc", False, False)] = (now, [_item("ABC-001")], 1)
    cache[("star", "other", False, True)] = (now, [_item("XYZ-001")], 1)

    missing_svc.invalidate_listing("star", "abc")

    assert ("star", "abc", False, True) not in cache
    assert ("star", "abc", False, False) not in cache
    assert ("star", "other", False, True) in cache  # untouched


async def test_delete_listing_removes_both_filter_rows(tmp_path, monkeypatch):
    await _bind_tmp_db(tmp_path, monkeypatch)
    for mag in (True, False):
        await listing_catalog.put(
            "star", "abc", uncensored=False, with_magnets_only=mag,
            items=[_item("ABC-001")], pages_scanned=1,
        )
    await listing_catalog.delete_listing("star", "abc")
    assert await listing_catalog.get("star", "abc", uncensored=False) is None
    assert (
        await listing_catalog.get(
            "star", "abc", uncensored=False, with_magnets_only=False
        )
        is None
    )


async def test_fetched_map_and_fetched_at(tmp_path, monkeypatch):
    await _bind_tmp_db(tmp_path, monkeypatch)
    await listing_catalog.put(
        "star", "abc", uncensored=False, with_magnets_only=True,
        items=[_item("ABC-001")], pages_scanned=1,
    )
    await listing_catalog.put(
        "star", "abc", uncensored=False, with_magnets_only=False,
        items=[_item("ABC-001")], pages_scanned=1,
    )
    m = await listing_catalog.fetched_map()
    assert set(m) == {("star", "abc")}  # only the mag row counts
    assert await listing_catalog.fetched_at("star", "abc") == m[("star", "abc")]
    assert await listing_catalog.fetched_at("star", "zzz") is None
