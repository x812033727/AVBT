"""Abandoned (dead-lettered) OfflineTaskLog rows are terminal but were
never covered by prune_offline_task_log's WHERE clause — it only looked
at archived=True rows. Left alone, abandoned rows (which have no
archived_at) accumulate forever. This exercises the retention window
against abandoned rows, keyed on created_at since they have no
archived_at, while leaving live (non-abandoned, non-archived) rows
untouched no matter their age."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.database as db
import app.services.log_cleanup as log_cleanup
from app.models import OfflineTaskLog


@pytest.fixture()
async def maker(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(log_cleanup, "SessionLocal", m)
    yield m
    await engine.dispose()


def _row(**kw):
    base = dict(
        code="X-001", magnet="magnet:?xt=1", btih="", task_id="t",
        file_id="", name="", phase="", message="", archived=False,
        archived_at=None, finalized=False, abandoned=False,
        created_at=datetime.utcnow(),
    )
    base.update(kw)
    return OfflineTaskLog(**base)


async def test_old_abandoned_row_is_pruned(maker):
    async with maker() as s:
        s.add(_row(code="OLD-ABANDONED", abandoned=True,
                   created_at=datetime.utcnow() - timedelta(days=100)))
        await s.commit()
    n = await log_cleanup.prune_offline_task_log(90)
    assert n == 1
    async with maker() as s:
        rows = (await s.execute(select(OfflineTaskLog))).scalars().all()
        assert rows == []


async def test_young_abandoned_row_is_kept(maker):
    async with maker() as s:
        s.add(_row(code="YOUNG-ABANDONED", abandoned=True,
                   created_at=datetime.utcnow() - timedelta(days=5)))
        await s.commit()
    n = await log_cleanup.prune_offline_task_log(90)
    assert n == 0
    async with maker() as s:
        row = (await s.execute(select(OfflineTaskLog))).scalar_one()
        assert row.code == "YOUNG-ABANDONED"


async def test_old_live_pending_row_is_never_pruned(maker):
    # Not abandoned, not archived — a live/pending row, however old, must
    # never be pruned; only terminal (archived or abandoned) rows are.
    async with maker() as s:
        s.add(_row(code="OLD-PENDING", abandoned=False, archived=False,
                   created_at=datetime.utcnow() - timedelta(days=200)))
        await s.commit()
    n = await log_cleanup.prune_offline_task_log(90)
    assert n == 0
    async with maker() as s:
        row = (await s.execute(select(OfflineTaskLog))).scalar_one()
        assert row.code == "OLD-PENDING"


async def test_old_archived_row_is_still_pruned(maker):
    # Existing behaviour must survive the WHERE-clause change.
    async with maker() as s:
        s.add(_row(code="OLD-ARCHIVED", archived=True,
                   archived_at=datetime.utcnow() - timedelta(days=100),
                   created_at=datetime.utcnow() - timedelta(days=100)))
        await s.commit()
    n = await log_cleanup.prune_offline_task_log(90)
    assert n == 1
    async with maker() as s:
        rows = (await s.execute(select(OfflineTaskLog))).scalars().all()
        assert rows == []
