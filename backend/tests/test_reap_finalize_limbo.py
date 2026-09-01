"""An archived row that ages past the finalize retry window, still has
files on PikPak, and is NOT flattened must still get a real finalize.

``_finalize_retry_pass`` selects archived rows on ``archived_at > cutoff``
(_FINALIZE_RETRY_WINDOW, 24h), so past that it never sees the row again.
``_reap_orphan_rows`` picks it up as the documented backstop, but only had
two exits: close it (files already flattened) or abandon it (nothing on
PikPak at all). A row whose wrapper is still sitting unflattened in the
系列 folder matched neither — it logged ``kept(archived, awaiting
finalize)`` and waited for a finalize pass that structurally can never
come again.

Live population 2026-08-05: OAE-044, NHDTA-152 x2, KAVR-415 x2,
NHDTA-326, PRVR-081 — archived 08-03, still unfinalized 44h later, their
BT-named wrappers still visible inside the 系列 folders.
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
import app.services.finalize as finalize_mod
import app.services.pikpak_presence as pp
from app.models import OfflineTaskLog


@pytest.fixture()
async def maker(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(archiver, "SessionLocal", m)
    monkeypatch.setattr(archiver, "_reap_attempts", {})
    monkeypatch.setattr(archiver, "_finalize_attempts", {})

    async def no_active():
        return set()

    async def not_flat(code, **kw):
        return False

    async def _noop_refresh(codes, **kw):
        return 0

    async def _something_landed(code, **kw):
        # Files ARE still on PikPak — not an abandon candidate.
        return False

    monkeypatch.setattr(archiver, "_active_task_ids", no_active)
    monkeypatch.setattr(archiver, "_already_flattened", not_flat)
    monkeypatch.setattr(archiver, "_orphan_has_nothing_landed", _something_landed)
    monkeypatch.setattr(pp.presence_index, "refresh_codes", _noop_refresh)
    yield m
    await engine.dispose()


def _aged_row(hours: int) -> OfflineTaskLog:
    """Archived `hours` ago, well past the 24h finalize retry window."""
    when = datetime.utcnow() - timedelta(hours=hours)
    return OfflineTaskLog(
        code="KAVR-415",
        magnet="magnet:?xt=urn:btih:deadbeef",
        task_id="gone",
        file_id="fid",
        name="wrapper",
        archived=True,
        archived_at=when,
        created_at=when,
        finalized=False,
        abandoned=False,
        superseded=False,
    )


async def test_aged_archived_row_gets_a_real_finalize(maker, monkeypatch):
    calls = []

    async def fake_finalize(service, code, **kw):
        calls.append((code, kw))
        return True

    monkeypatch.setattr(finalize_mod, "run_finalize", fake_finalize)

    async with maker() as s:
        s.add(_aged_row(44))
        await s.commit()

    await archiver._reap_orphan_rows()

    assert [c for c, _ in calls] == ["KAVR-415"], (
        "reaper must hand an aged archived row back to finalize; "
        "the retry pass can no longer select it"
    )
    # 44h > _ABANDON_GRACE (24h) → shell-trash is opted in, matching what
    # _finalize_retry_pass would have passed inside the window.
    assert calls[0][1].get("allow_shell_trash") is True

    async with maker() as s:
        row = (await s.execute(select(OfflineTaskLog))).scalars().one()
    assert row.finalized is True
    assert row.finalized_at is not None
    assert row.abandoned is False


async def test_finalize_failure_leaves_row_open_not_abandoned(maker, monkeypatch):
    """A finalize that cannot complete yet must not close or abandon the
    row — it stays pending so the next pass retries."""

    async def fake_finalize(service, code, **kw):
        return False

    monkeypatch.setattr(finalize_mod, "run_finalize", fake_finalize)

    async with maker() as s:
        s.add(_aged_row(44))
        await s.commit()

    await archiver._reap_orphan_rows()

    async with maker() as s:
        row = (await s.execute(select(OfflineTaskLog))).scalars().one()
    assert row.finalized is False
    assert row.abandoned is False


async def test_finalize_error_does_not_close_the_row(maker, monkeypatch):
    """A raising finalize is an unreliable signal, never a close."""

    async def boom(service, code, **kw):
        raise RuntimeError("PikPak 500")

    monkeypatch.setattr(finalize_mod, "run_finalize", boom)

    async with maker() as s:
        s.add(_aged_row(44))
        await s.commit()

    await archiver._reap_orphan_rows()

    async with maker() as s:
        row = (await s.execute(select(OfflineTaskLog))).scalars().one()
    assert row.finalized is False
    assert row.abandoned is False


async def test_unarchived_orphan_is_not_handed_to_finalize(maker, monkeypatch):
    """Only the aged-archived branch changes. A never-archived Collecting
    orphan keeps its existing disposition (the retry pass still owns it
    for the whole reap window)."""
    calls = []

    async def fake_finalize(service, code, **kw):
        calls.append(code)
        return True

    monkeypatch.setattr(finalize_mod, "run_finalize", fake_finalize)

    row = _aged_row(44)
    row.archived = False
    row.archived_at = None
    async with maker() as s:
        s.add(row)
        await s.commit()

    await archiver._reap_orphan_rows()

    assert calls == []
    async with maker() as s:
        got = (await s.execute(select(OfflineTaskLog))).scalars().one()
    assert got.finalized is False
