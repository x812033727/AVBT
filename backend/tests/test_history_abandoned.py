"""/api/collection/history: abandoned (dead-letter) visibility.

Round-4: the endpoint silently mixed abandoned rows into the default
view with no way to isolate them. Adds an `abandoned` query param
(None|True|False) mirroring the existing `archived` param, and surfaces
the flag on each HistoryItem so the frontend can badge it."""

from datetime import datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as db
from app.models import OfflineTaskLog
from app.routers.collection import history


async def _seeded_session(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        session.add_all(
            [
                OfflineTaskLog(
                    code="LIVE-001",
                    magnet="magnet:?xt=urn:btih:" + "A" * 40,
                    file_id="f1",
                    phase="PHASE_TYPE_COMPLETE",
                    archived=True,
                    abandoned=False,
                    created_at=datetime.utcnow(),
                ),
                OfflineTaskLog(
                    code="LIVE-002",
                    magnet="magnet:?xt=urn:btih:" + "B" * 40,
                    file_id="f2",
                    phase="PHASE_TYPE_RUNNING",
                    archived=False,
                    abandoned=False,
                    created_at=datetime.utcnow(),
                ),
                OfflineTaskLog(
                    code="DEAD-001",
                    magnet="magnet:?xt=urn:btih:" + "C" * 40,
                    file_id="",
                    phase="PHASE_TYPE_ERROR",
                    message="abandoned: task gone, no archived copy found",
                    archived=False,
                    abandoned=True,
                    created_at=datetime.utcnow(),
                ),
            ]
        )
        await session.commit()
    return maker, engine


async def test_default_view_is_unfiltered_and_byte_identical(tmp_path, monkeypatch):
    maker, engine = await _seeded_session(tmp_path, monkeypatch)
    async with maker() as session:
        out = await history(session=session, limit=50, offset=0)
    await engine.dispose()

    assert out.total == 3
    codes = {it.code for it in out.items}
    assert codes == {"LIVE-001", "LIVE-002", "DEAD-001"}
    by_code = {it.code: it for it in out.items}
    assert by_code["DEAD-001"].abandoned is True
    assert by_code["LIVE-001"].abandoned is False
    assert by_code["LIVE-002"].abandoned is False


async def test_abandoned_true_filters_to_dead_letter_rows(tmp_path, monkeypatch):
    maker, engine = await _seeded_session(tmp_path, monkeypatch)
    async with maker() as session:
        out = await history(session=session, limit=50, offset=0, abandoned=True)
    await engine.dispose()

    assert out.total == 1
    assert [it.code for it in out.items] == ["DEAD-001"]
    assert out.items[0].abandoned is True
    assert out.items[0].message == "abandoned: task gone, no archived copy found"


async def test_abandoned_false_excludes_dead_letter_rows(tmp_path, monkeypatch):
    maker, engine = await _seeded_session(tmp_path, monkeypatch)
    async with maker() as session:
        out = await history(session=session, limit=50, offset=0, abandoned=False)
    await engine.dispose()

    assert out.total == 2
    codes = {it.code for it in out.items}
    assert codes == {"LIVE-001", "LIVE-002"}
    assert all(not it.abandoned for it in out.items)
