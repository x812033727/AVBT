"""Works moved to Google Drive (PikPak source trashed) still count as
downloaded: in the 缺漏 badge, the listing pages' present_codes, tracker
auto-send backfill and collection sync-status. Before this, a moved work
read as missing and auto-send re-downloaded it — then gdrive_auto moved
it again, forever."""

import asyncio
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.database as db
import app.routers.collection as collection_router
import app.services.tracker as tracker
from app.models import CollectedMovie, GDriveLocation
from app.services import missing as missing_svc
from app.services.missing import MovieListItem


async def _bind(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(missing_svc, "SessionLocal", maker)
    async with maker() as s:
        # ABC-002 lives only on Drive (moved, PikPak copy trashed).
        s.add(GDriveLocation(code="ABC-002", account_id=1, drive_file_id="F1", path="p"))
        await s.commit()
    return engine, maker


def _stub_catalog(monkeypatch):
    async def fake_fetch(kind, slug, *, uncensored=False, refresh=False, **kw):
        return ([MovieListItem(code=c, title=c) for c in ("ABC-001", "ABC-002", "ABC-003")], 1)

    async def no_owned(*a, **kw):
        return []

    async def fake_presence(*, force=False):
        return {"ABC-001"}

    monkeypatch.setattr(missing_svc, "fetch_all_listing_codes", fake_fetch)
    monkeypatch.setattr(missing_svc, "_owned_listing_members", no_owned)
    monkeypatch.setattr(missing_svc.presence_index, "get", fake_presence)


async def test_drive_codes_count_as_present(tmp_path, monkeypatch):
    engine, _ = await _bind(tmp_path, monkeypatch)
    _stub_catalog(monkeypatch)
    res = await missing_svc.missing_for_listing("studio", "abc", dedup=False)
    assert sorted(res.present_codes) == ["ABC-001", "ABC-002"]
    assert [m.code for m in res.missing] == ["ABC-003"]
    assert await missing_svc._owned_codes() == {"ABC-001", "ABC-002"}
    await engine.dispose()


async def test_summary_item_excludes_drive_codes(tmp_path, monkeypatch):
    engine, _ = await _bind(tmp_path, monkeypatch)
    _stub_catalog(monkeypatch)
    row = SimpleNamespace(kind="studio", id="abc", name="ABC", uncensored=False)
    item = await missing_svc._summary_item(row, await missing_svc._owned_codes())
    assert item.total == 3 and item.missing_count == 1
    await engine.dispose()


async def test_tracker_backfill_skips_drive_codes(tmp_path, monkeypatch):
    engine, _ = await _bind(tmp_path, monkeypatch)
    _stub_catalog(monkeypatch)
    jobs = []

    class _Queue:
        async def enqueue(self, job):
            jobs.append(job)
            fut = asyncio.get_event_loop().create_future()
            fut.set_result(SimpleNamespace(status="sent"))
            return fut

    async def nothing(*a, **kw):
        return set()

    async def no_record(*a, **kw):
        return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, model, key):
            return None

    monkeypatch.setattr(tracker, "download_queue", _Queue())
    monkeypatch.setattr(tracker.presence_index, "status", lambda: {"ready": True, "last_error": ""})
    monkeypatch.setattr(tracker, "_recently_submitted_codes", nothing)
    monkeypatch.setattr(tracker, "_record_scan_result", no_record)
    monkeypatch.setattr(tracker, "SessionLocal", _Session)
    monkeypatch.setattr(tracker, "state", tracker.TrackerState())

    await tracker._enqueue_auto_send("studio", "abc", [])
    assert [j.code for j in jobs] == ["ABC-003"]  # not the Drive-only ABC-002
    await engine.dispose()


async def test_sync_status_marks_drive_codes_done(tmp_path, monkeypatch):
    engine, maker = await _bind(tmp_path, monkeypatch)

    async def fake_presence(*, force=False):
        return set()

    monkeypatch.setattr(collection_router.presence_index, "get", fake_presence)
    async with maker() as s:
        s.add(CollectedMovie(code="ABC-002", title="t", status="wishlist"))
        await s.commit()
        await collection_router.sync_status(session=s)
    async with maker() as s:
        row = (await s.execute(select(CollectedMovie))).scalars().one()
    assert row.status == "done"
    await engine.dispose()
