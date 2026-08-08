"""Timing instrumentation for the finalize retry pass.

The pass got ~4x slower between 2026-08-03 and 2026-08-06 (archived→
finalized median 8.6 min → 27.8 min) and the existing logs cannot say
where the time went: a pass that drains 3 rows and a pass that drains 30
look identical. These tests pin the observability contract — one summary
line per pass carrying stage timings and the PikPak round-trip count —
so the next regression is diagnosable from the log alone.
"""

import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import OfflineTaskLog
from app.services import archiver as arch
from app.services import finalize as fin
from app.services.pikpak import PikPakService

# ---------------------------------------------------------------------------
# PikPak round-trip counter
# ---------------------------------------------------------------------------

async def test_call_counter_increments_per_round_trip(monkeypatch):
    """``api_call_total`` is the denominator for "is this pass slow
    because it does more work, or because each call got slower?"."""
    svc = PikPakService()

    async def fake_ensure():
        return SimpleNamespace()

    monkeypatch.setattr(svc, "_ensure", fake_ensure)
    monkeypatch.setattr("app.services.pikpak.settings",
                        SimpleNamespace(pikpak_api_timeout_seconds=0,
                                        pikpak_throttle_max_retries=0,
                                        pikpak_throttle_base_seconds=0,
                                        pikpak_throttle_max_seconds=0))

    assert svc.api_call_total == 0
    await svc._call(lambda c: _ok())
    await svc._call(lambda c: _ok())
    assert svc.api_call_total == 2


async def _ok():
    return {}


async def test_call_counter_counts_failed_round_trips(monkeypatch):
    """A throttled or timed-out call still consumed a round trip — not
    counting it would understate load exactly when load is the problem."""
    svc = PikPakService()

    async def fake_ensure():
        return SimpleNamespace()

    monkeypatch.setattr(svc, "_ensure", fake_ensure)
    monkeypatch.setattr("app.services.pikpak.settings",
                        SimpleNamespace(pikpak_api_timeout_seconds=0,
                                        pikpak_throttle_max_retries=0,
                                        pikpak_throttle_base_seconds=0,
                                        pikpak_throttle_max_seconds=0))

    async def boom():
        raise RuntimeError("nope")

    try:
        await svc._call(lambda c: boom())
    except Exception:  # noqa: BLE001 — the raise is the point
        pass
    assert svc.api_call_total == 1


# ---------------------------------------------------------------------------
# finalize pass summary line
# ---------------------------------------------------------------------------

async def _retry_db(tmp_path, monkeypatch, rows, *, refresh_calls=None):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(arch, "SessionLocal", maker)

    from app.services.pikpak_presence import presence_index

    async def fake_refresh(codes):
        if refresh_calls is not None:
            refresh_calls.append(list(codes))
        return 0

    monkeypatch.setattr(presence_index, "refresh_codes", fake_refresh)
    arch._finalize_attempts.clear()
    arch._reap_attempts.clear()
    async with maker() as s:
        s.add_all(rows)
        await s.commit()
    return engine, maker


async def test_pass_emits_summary_with_stage_timings(tmp_path, monkeypatch, caplog):
    now = datetime.utcnow()
    engine, maker = await _retry_db(tmp_path, monkeypatch, [
        OfflineTaskLog(code="MIDV-001", magnet="m", archived=True,
                       archived_at=now, finalized=False,
                       created_at=now - timedelta(hours=1)),
        OfflineTaskLog(code="MIDV-002", magnet="m", archived=True,
                       archived_at=now, finalized=False,
                       created_at=now - timedelta(hours=1)),
    ])

    async def fake_run_finalize(svc, code, *, folder_id=None, **_kw):
        return {"errors": 0}

    async def no_active():
        return set()

    monkeypatch.setattr(fin, "run_finalize", fake_run_finalize)
    monkeypatch.setattr(arch, "_active_task_ids", no_active)

    with caplog.at_level(logging.INFO, logger="app.services.archiver"):
        done = await arch._finalize_retry_pass()

    assert done == 2
    summary = [r for r in caplog.records if "finalize pass:" in r.getMessage()]
    assert len(summary) == 1, "exactly one summary line per pass"
    msg = summary[0].getMessage()
    # Selected vs candidates vs drained: a pass that selects 200 rows and
    # drains 2 is a very different failure from one that selects 2.
    for field in ("selected=", "candidates=", "done=", "elapsed=",
                  "presence=", "tasklist=", "rows=", "pikpak_calls="):
        assert field in msg, f"{field!r} missing from summary: {msg}"

    async with maker() as s:
        rows = {r.code: r for r in (await s.execute(select(OfflineTaskLog))).scalars()}
    assert all(r.finalized for r in rows.values())
    await engine.dispose()


async def test_summary_still_emitted_when_nothing_drains(tmp_path, monkeypatch, caplog):
    """The silent case is the one worth seeing: every candidate on
    cooldown, or every row still downloading, spends a full pass and
    finalizes nothing. Without a line here the log shows only absence."""
    now = datetime.utcnow()
    engine, _maker = await _retry_db(tmp_path, monkeypatch, [
        OfflineTaskLog(code="MIDV-001", magnet="m", task_id="t-run",
                       archived=True, archived_at=now, finalized=False,
                       created_at=now - timedelta(hours=1)),
    ])

    async def still_running():
        return {"t-run"}

    monkeypatch.setattr(arch, "_active_task_ids", still_running)

    with caplog.at_level(logging.INFO, logger="app.services.archiver"):
        done = await arch._finalize_retry_pass()

    assert done == 0
    summary = [r for r in caplog.records if "finalize pass:" in r.getMessage()]
    assert len(summary) == 1
    msg = summary[0].getMessage()
    assert "selected=1" in msg
    assert "candidates=0" in msg
    assert "done=0" in msg
    await engine.dispose()


async def test_summary_reports_presence_skip_as_its_own_outcome(
    tmp_path, monkeypatch, caplog
):
    """A presence refresh that raises aborts the whole pass. That is a
    100%-wasted cycle and must not read the same as "nothing to do"."""
    now = datetime.utcnow()
    engine, _maker = await _retry_db(tmp_path, monkeypatch, [
        OfflineTaskLog(code="MIDV-001", magnet="m", archived=True,
                       archived_at=now, finalized=False,
                       created_at=now - timedelta(hours=1)),
    ])

    from app.services.pikpak_presence import presence_index

    async def boom(codes):
        raise RuntimeError("presence down")

    async def no_active():
        return set()

    monkeypatch.setattr(presence_index, "refresh_codes", boom)
    monkeypatch.setattr(arch, "_active_task_ids", no_active)

    with caplog.at_level(logging.INFO, logger="app.services.archiver"):
        done = await arch._finalize_retry_pass()

    assert done == 0
    summary = [r for r in caplog.records if "finalize pass:" in r.getMessage()]
    assert len(summary) == 1
    assert "aborted=presence" in summary[0].getMessage()
    await engine.dispose()
