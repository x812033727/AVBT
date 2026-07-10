import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as db
import app.routers.collection as collection_router
from app.models import CollectedMovie, OfflineTaskLog


@pytest.fixture
async def fresh_db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/sync.db", echo=False, future=True
    )
    session_local = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", session_local)
    await db.init_db()
    yield session_local
    await engine.dispose()


@pytest.fixture
def presence(monkeypatch):
    codes: set[str] = set()

    async def fake_get(force: bool = False):
        return codes

    monkeypatch.setattr(collection_router.presence_index, "get", fake_get)
    return codes


async def _seed(session_local, *, collected, logs=()):
    async with session_local() as session:
        for code, status in collected:
            session.add(CollectedMovie(code=code, title=code, status=status))
        for code, archived in logs:
            session.add(
                OfflineTaskLog(code=code, magnet="magnet:?", archived=archived)
            )
        await session.commit()


async def _sync(session_local):
    async with session_local() as session:
        return await collection_router.sync_status(session=session)


async def _status_of(session_local, code):
    async with session_local() as session:
        return (await session.get(CollectedMovie, code)).status


async def test_forward_transitions(fresh_db, presence):
    presence.add("CCC-333")
    await _seed(
        fresh_db,
        collected=[
            ("AAA-111", "wishlist"),   # no cloud trace → stays
            ("BBB-222", "wishlist"),   # sent → downloading
            ("CCC-333", "wishlist"),   # in presence → done
            ("DDD-444", "downloading"),  # archived log → done
        ],
        logs=[("BBB-222", False), ("DDD-444", True)],
    )

    r = await _sync(fresh_db)

    assert r == {"checked": 4, "to_downloading": 1, "to_done": 2}
    assert await _status_of(fresh_db, "AAA-111") == "wishlist"
    assert await _status_of(fresh_db, "BBB-222") == "downloading"
    assert await _status_of(fresh_db, "CCC-333") == "done"
    assert await _status_of(fresh_db, "DDD-444") == "done"


async def test_never_downgrades(fresh_db, presence):
    # done + only a non-archived log (would map to "downloading") → stays done.
    await _seed(
        fresh_db,
        collected=[("EEE-555", "done")],
        logs=[("EEE-555", False)],
    )
    r = await _sync(fresh_db)
    assert r["to_downloading"] == 0 and r["to_done"] == 0
    assert await _status_of(fresh_db, "EEE-555") == "done"
