"""Fossil reconcile: mark historical offline_task_log rows superseded
when their code is already archived/finalized elsewhere (a real video
already sits on PikPak, or a sibling row already finalized it), so they
stop holding archive_once's pending peek open and stop being re-listed
by the finalize retry / reap passes. Strongest evidence first: presence
of an actual video/container file outranks a merely-finalized sibling
row (whose own finalize may since have been undone by a re-download)."""

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.database as db
from app.models import OfflineTaskLog, PresenceEntry
from app.services import log_reconcile

# Default older_than cutoff is "2026-07-01" — well after this fixture date,
# so rows created here are candidates unless a test says otherwise.
OLD = datetime(2026, 1, 1)


@pytest.fixture()
async def maker(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(log_reconcile, "SessionLocal", m)
    yield m
    await engine.dispose()


def _row(**kw):
    base = dict(
        code="X-001", magnet="magnet:?xt=1", btih="", task_id="", file_id="",
        name="", phase="", message="", archived=False, finalized=False,
        abandoned=False, superseded=False, created_at=OLD,
    )
    base.update(kw)
    return OfflineTaskLog(**base)


async def _get(maker, code):
    async with maker() as s:
        return (
            await s.execute(
                select(OfflineTaskLog).where(
                    OfflineTaskLog.code == code, OfflineTaskLog.finalized.is_(False)
                )
            )
        ).scalar_one()


async def test_presence_video_marks_superseded(maker):
    async with maker() as s:
        s.add(_row(code="VID-001"))
        s.add(PresenceEntry(code="VID-001", path="AVBT/系列/S/VID-001/VID-001.mp4"))
        await s.commit()

    result = await log_reconcile.reconcile_superseded(dry_run=False)

    assert result["scanned"] == 1
    assert result["presence_video"] == 1
    assert result["finalized_sibling"] == 0
    assert result["untouched"] == 0
    row = await _get(maker, "VID-001")
    assert row.superseded is True
    assert row.message == "superseded: presence has video"


async def test_presence_folder_only_path_does_not_match_video_rule(maker):
    async with maker() as s:
        s.add(_row(code="FOLD-001"))
        # Folder-only listing: the leaf IS the code, no file extension.
        s.add(PresenceEntry(code="FOLD-001", path="AVBT/系列/S/FOLD-001"))
        await s.commit()

    result = await log_reconcile.reconcile_superseded(dry_run=False)

    assert result["presence_video"] == 0
    assert result["untouched"] == 1
    row = await _get(maker, "FOLD-001")
    assert row.superseded is False


async def test_finalized_sibling_marks_superseded(maker):
    async with maker() as s:
        s.add(_row(code="SIB-001", task_id="t1"))
        s.add(
            _row(
                code="SIB-001", task_id="t2", finalized=True,
                created_at=datetime.utcnow(),
            )
        )
        await s.commit()

    result = await log_reconcile.reconcile_superseded(dry_run=False)

    # Only the non-finalized row is a candidate; its finalized sibling
    # isn't itself scanned (finalized=1 fails the candidate filter).
    assert result["scanned"] == 1
    assert result["finalized_sibling"] == 1
    row = await _get(maker, "SIB-001")
    assert row.superseded is True
    assert row.message == "superseded: finalized sibling"


async def test_presence_video_outranks_finalized_sibling(maker):
    async with maker() as s:
        s.add(_row(code="BOTH-001", task_id="t1"))
        s.add(
            _row(
                code="BOTH-001", task_id="t2", finalized=True,
                created_at=datetime.utcnow(),
            )
        )
        s.add(PresenceEntry(code="BOTH-001", path="AVBT/系列/S/BOTH-001/BOTH-001.mkv"))
        await s.commit()

    result = await log_reconcile.reconcile_superseded(dry_run=False)

    assert result["presence_video"] == 1
    assert result["finalized_sibling"] == 0
    row = await _get(maker, "BOTH-001")
    assert row.message == "superseded: presence has video"


async def test_no_evidence_left_untouched(maker):
    async with maker() as s:
        s.add(_row(code="NONE-001"))
        await s.commit()

    result = await log_reconcile.reconcile_superseded(dry_run=False)

    assert result["untouched"] == 1
    row = await _get(maker, "NONE-001")
    assert row.superseded is False
    assert row.message == ""


async def test_dry_run_touches_nothing(maker):
    async with maker() as s:
        s.add(_row(code="VID-002"))
        s.add(PresenceEntry(code="VID-002", path="AVBT/系列/S/VID-002/VID-002.mkv"))
        await s.commit()

    result = await log_reconcile.reconcile_superseded(dry_run=True)

    assert result["dry_run"] is True
    assert result["presence_video"] == 1
    row = await _get(maker, "VID-002")
    assert row.superseded is False
    assert row.message == ""


async def test_older_than_excludes_recent_rows(maker):
    async with maker() as s:
        s.add(_row(code="FRESH-001", created_at=datetime.utcnow()))
        await s.commit()

    result = await log_reconcile.reconcile_superseded(dry_run=False)

    assert result["scanned"] == 0


async def test_older_than_is_hard_required_date_not_relative(maker):
    async with maker() as s:
        s.add(_row(code="ANY-001"))
        await s.commit()

    with pytest.raises(ValueError):
        await log_reconcile.reconcile_superseded(dry_run=True, older_than="7d")


async def test_limit_bounds_scanned_rows(maker):
    async with maker() as s:
        for i in range(5):
            s.add(_row(code=f"LIM-{i:03d}"))
        await s.commit()

    result = await log_reconcile.reconcile_superseded(dry_run=True, limit=2)

    assert result["scanned"] == 2
