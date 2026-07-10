from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as db
import app.services.pcloud_transfer as pt
from app.models import PCloudTransfer


@pytest.fixture
async def fresh_db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/transfer.db", echo=False, future=True
    )
    session_local = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", session_local)
    monkeypatch.setattr(pt, "SessionLocal", session_local)
    await db.init_db()
    yield session_local
    await engine.dispose()


@pytest.fixture
def sent(monkeypatch):
    out: list[tuple[str, str]] = []
    monkeypatch.setattr(
        pt.webhook_queue,
        "enqueue_nowait",
        lambda msg, event="generic": out.append((event, msg)),
    )
    return out


async def _add_row(session_local, **kw) -> int:
    async with session_local() as session:
        row = PCloudTransfer(
            pikpak_file_id="f1", pikpak_name="movie.mp4", status="running", **kw
        )
        session.add(row)
        await session.commit()
        return row.id


async def _get(session_local, rid) -> PCloudTransfer:
    async with session_local() as session:
        return await session.get(PCloudTransfer, rid)


async def test_transient_failure_requeues_with_backoff(fresh_db, sent, monkeypatch):
    monkeypatch.setattr(pt.settings, "pcloud_transfer_max_attempts", 3)
    monkeypatch.setattr(pt.settings, "pcloud_transfer_retry_base_seconds", 60)
    queue = pt.PCloudTransferQueue()
    rid = await _add_row(fresh_db)

    await queue._fail_or_retry(rid, "pCloud 錯誤: boom")

    row = await _get(fresh_db, rid)
    assert row.status == "pending"
    assert row.attempts == 1
    assert row.pcloud_upload_id == 0
    assert row.next_retry_at is not None
    assert row.next_retry_at > datetime.utcnow() + timedelta(seconds=30)
    assert "自動重試" in row.message
    assert sent == []  # not final yet — no notification


async def test_exhausted_attempts_fail_and_notify(fresh_db, sent, monkeypatch):
    monkeypatch.setattr(pt.settings, "pcloud_transfer_max_attempts", 2)
    queue = pt.PCloudTransferQueue()
    rid = await _add_row(fresh_db, attempts=1)  # one strike already

    await queue._fail_or_retry(rid, "pCloud 錯誤: boom")

    row = await _get(fresh_db, rid)
    assert row.status == "failed"
    assert row.finished_at is not None
    assert sent and sent[0][0] == "transfer_failed"
    assert "movie.mp4" in sent[0][1]


async def test_claim_skips_rows_still_backing_off(fresh_db, monkeypatch):
    queue = pt.PCloudTransferQueue()
    future = datetime.utcnow() + timedelta(seconds=600)
    past = datetime.utcnow() - timedelta(seconds=5)
    async with fresh_db() as session:
        session.add(PCloudTransfer(pikpak_file_id="a", status="pending",
                                   next_retry_at=future))
        session.add(PCloudTransfer(pikpak_file_id="b", status="pending",
                                   next_retry_at=past))
        session.add(PCloudTransfer(pikpak_file_id="c", status="pending"))
        await session.commit()

    claimed = await queue._claim_pending(10)

    async with fresh_db() as session:
        claimed_fids = (
            await session.execute(
                select(PCloudTransfer.pikpak_file_id).where(
                    PCloudTransfer.id.in_(claimed)
                )
            )
        ).scalars().all()
    assert sorted(claimed_fids) == ["b", "c"]  # future-backoff row skipped


async def test_manual_retry_resets_attempt_budget(fresh_db, monkeypatch):
    queue = pt.PCloudTransferQueue()
    rid = await _add_row(fresh_db, attempts=3)
    async with fresh_db() as session:
        row = await session.get(PCloudTransfer, rid)
        row.status = "failed"
        await session.commit()

    assert await queue.retry(rid)

    row = await _get(fresh_db, rid)
    assert row.status == "pending"
    assert row.attempts == 0
    assert row.next_retry_at is None
