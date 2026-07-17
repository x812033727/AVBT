"""Dead-letter genuinely-dead orphan rows so the finalize retry pass
stops re-listing them every ~10 min for the 7-day reap window.

A row is abandoned only when the download never produced a file
(file_id empty), the task is gone from PikPak, the code is NOT at the
destination (not flattened — the safety gate against abandoning a
landed-but-unstamped row), and it is older than the 24h grace.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.database as db
import app.services.archiver as archiver
from app.models import OfflineTaskLog


@pytest.fixture()
async def maker(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(archiver, "SessionLocal", m)
    # Reset module-global attempt maps so a row.id (restarts at 1 per fresh
    # tmp DB) from one test can't skip the reaper/retry loop in the next.
    monkeypatch.setattr(archiver, "_reap_attempts", {})
    monkeypatch.setattr(archiver, "_finalize_attempts", {})
    # No live PikPak: no active tasks; nothing is flattened by default.
    async def no_active():
        return set()

    async def not_flat(code):
        return False

    monkeypatch.setattr(archiver, "_active_task_ids", no_active)
    monkeypatch.setattr(archiver, "_already_flattened", not_flat)
    yield m
    await engine.dispose()


def _row(**kw):
    base = dict(
        code="X-001", magnet="magnet:?xt=1", btih="", task_id="gone",
        file_id="", name="", phase="", message="", archived=False,
        finalized=False,
        created_at=datetime.utcnow() - timedelta(hours=26),
    )
    base.update(kw)
    return OfflineTaskLog(**base)


async def test_dead_orphan_is_abandoned(maker):
    async with maker() as s:
        s.add(_row(code="DEAD-001"))
        await s.commit()
    await archiver._reap_orphan_rows()
    async with maker() as s:
        row = (await s.execute(
            select(OfflineTaskLog).where(OfflineTaskLog.code == "DEAD-001")
        )).scalar_one()
        assert row.abandoned is True
        assert row.finalized is False          # abandoned, not "done"


async def test_flattened_row_is_finalized_not_abandoned(maker, monkeypatch):
    async def yes_flat(code):
        return True

    monkeypatch.setattr(archiver, "_already_flattened", yes_flat)
    async with maker() as s:
        s.add(_row(code="LAND-001"))
        await s.commit()
    await archiver._reap_orphan_rows()
    async with maker() as s:
        row = (await s.execute(
            select(OfflineTaskLog).where(OfflineTaskLog.code == "LAND-001")
        )).scalar_one()
        assert row.abandoned is False          # never abandon a landed row
        assert row.finalized is True           # existing stamp behaviour


async def test_fresh_row_within_grace_is_left(maker):
    async with maker() as s:
        s.add(_row(code="FRESH-001",
                   created_at=datetime.utcnow() - timedelta(hours=2)))
        await s.commit()
    await archiver._reap_orphan_rows()
    async with maker() as s:
        row = (await s.execute(
            select(OfflineTaskLog).where(OfflineTaskLog.code == "FRESH-001")
        )).scalar_one()
        assert row.abandoned is False
        assert row.finalized is False


async def test_row_with_file_id_not_abandoned(maker):
    async with maker() as s:
        s.add(_row(code="HASFILE-001", file_id="f-123"))
        await s.commit()
    await archiver._reap_orphan_rows()
    async with maker() as s:
        row = (await s.execute(
            select(OfflineTaskLog).where(OfflineTaskLog.code == "HASFILE-001")
        )).scalar_one()
        assert row.abandoned is False          # out of scope (has a file)


async def test_abandoned_row_excluded_from_retry_and_reap(maker):
    async with maker() as s:
        s.add(_row(code="GONE-001", abandoned=True))
        await s.commit()
    # Neither pass should select an already-abandoned row.
    n_reap = await archiver._reap_orphan_rows()
    n_retry = await archiver._finalize_retry_pass()
    assert n_reap == 0
    assert n_retry == 0
