"""立即檢查 walk-vs-re-derive decision (``_needs_catalog_walk``).

A manual check used to force a full JavBus catalog re-walk on every
click (``_record_missing_count`` hardcoded refresh=True). Now it walks
only when the persisted catalog is stale or doesn't contain page-1's
newest code; otherwise the missing set is re-derived from the DB.
"""

from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as db
from app.models import TrackedListing
from app.schemas import MovieListItem
from app.services import listing_catalog, tracker
from app.services import missing as missing_svc


async def _bind_tmp_db(tmp_path, monkeypatch, name="t.db"):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/{name}", future=True
    )
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", maker)
    monkeypatch.setattr(missing_svc, "SessionLocal", maker)
    monkeypatch.setattr(listing_catalog, "SessionLocal", maker)
    monkeypatch.setattr(tracker, "SessionLocal", maker)
    monkeypatch.setattr(missing_svc, "_listing_cache", {})
    monkeypatch.setattr(missing_svc, "_walk_locks", {})
    return maker


def _item(code: str) -> MovieListItem:
    return MovieListItem(code=code, title=code)


async def _seed_row(maker, *, last_seen="ABC-002"):
    async with maker() as s:
        s.add(
            TrackedListing(
                kind="star", id="abc", name="Abc",
                uncensored=False, auto_send=False,
                last_seen_code=last_seen,
            )
        )
        await s.commit()


def _stub_page1(monkeypatch, top_code="ABC-002"):
    async def fake_fetch_listing(kind, slug, *, page=1, uncensored=False, **kw):
        return SimpleNamespace(items=[_item(top_code)])

    monkeypatch.setattr(tracker.scraper, "fetch_listing", fake_fetch_listing)


def _record_scans(monkeypatch):
    calls: list[bool] = []

    async def fake_missing_for_listing(kind, slug, *, refresh=False, **kw):
        calls.append(refresh)
        return SimpleNamespace(missing=[], total=1)

    monkeypatch.setattr(
        missing_svc, "missing_for_listing", fake_missing_for_listing
    )
    return calls


async def _drive_stream(**kw):
    events = []
    async for ev in tracker.check_listing_stream("star", "abc", **kw):
        events.append(ev)
    return events


async def test_current_catalog_skips_walk(tmp_path, monkeypatch):
    maker = await _bind_tmp_db(tmp_path, monkeypatch)
    await _seed_row(maker)
    await listing_catalog.put(
        "star", "abc", uncensored=False, with_magnets_only=True,
        items=[_item("ABC-001"), _item("ABC-002")], pages_scanned=1,
    )
    _stub_page1(monkeypatch, top_code="ABC-002")
    calls = _record_scans(monkeypatch)

    events = await _drive_stream(force=True)

    assert calls == [False], "fresh catalog with page-1 code → re-derive"
    done = [e for e in events if e["type"] == "done"][0]
    assert not done.get("error")
    msgs = [e.get("message", "") for e in events if e["type"] == "progress"]
    assert any("既有目錄" in m for m in msgs)


async def test_stale_catalog_walks(tmp_path, monkeypatch):
    maker = await _bind_tmp_db(tmp_path, monkeypatch)
    await _seed_row(maker)
    # Catalog exists but page-1's newest code is NOT in it → stale.
    await listing_catalog.put(
        "star", "abc", uncensored=False, with_magnets_only=True,
        items=[_item("ABC-001")], pages_scanned=1,
    )
    _stub_page1(monkeypatch, top_code="ABC-002")
    calls = _record_scans(monkeypatch)

    await _drive_stream(force=True)

    assert calls == [True], "newest code missing from catalog → walk"


async def test_no_catalog_walks(tmp_path, monkeypatch):
    maker = await _bind_tmp_db(tmp_path, monkeypatch)
    await _seed_row(maker)
    _stub_page1(monkeypatch, top_code="ABC-002")
    calls = _record_scans(monkeypatch)

    await _drive_stream(force=True)

    assert calls == [True], "no persisted catalog → walk"
