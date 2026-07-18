"""archive_once finalizes per-distinct-code concurrently (bounded), keeping
all moves/session mutations serial. Concurrency=1 reproduces the old
serial behaviour. Per-code finalize failure is isolated; only rows that
actually moved get finalized; duplicate-code rows finalize once."""

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.database as db
import app.services.archiver as arch
from app.models import OfflineTaskLog

# ---------- _run_finalize_batch (the concurrency mechanism) ----------

async def test_finalize_batch_returns_ok_codes(monkeypatch):
    async def fake_finalize(svc, code, *, folder_id=None):
        return code != "FAIL-001"  # everything ok except one

    monkeypatch.setattr("app.services.finalize.run_finalize", fake_finalize)
    targets = {"A-1": "t1", "B-2": "t2", "FAIL-001": "t3"}
    ok = await arch._run_finalize_batch(targets, 2)
    assert ok == {"A-1", "B-2"}


async def test_finalize_batch_isolates_exceptions(monkeypatch):
    async def fake_finalize(svc, code, *, folder_id=None):
        if code == "BOOM-1":
            raise RuntimeError("pikpak blew up")
        return True

    monkeypatch.setattr("app.services.finalize.run_finalize", fake_finalize)
    ok = await arch._run_finalize_batch({"BOOM-1": "t1", "OK-2": "t2"}, 2)
    assert ok == {"OK-2"}          # one raise never aborts the batch


async def test_finalize_batch_bounds_concurrency(monkeypatch):
    active = 0
    peak = 0

    async def fake_finalize(svc, code, *, folder_id=None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)     # yield so overlap is possible
        active -= 1
        return True

    monkeypatch.setattr("app.services.finalize.run_finalize", fake_finalize)
    await arch._run_finalize_batch({f"C-{i}": f"t{i}" for i in range(10)}, 3)
    assert peak <= 3               # semaphore caps in-flight


async def test_finalize_batch_empty():
    assert await arch._run_finalize_batch({}, 4) == set()


# ---------- archive_once integration (dedup + over-mark + serial moves) --

async def _archive_db(tmp_path, monkeypatch, rows):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(arch, "SessionLocal", m)
    async with m() as s:
        for r in rows:
            s.add(r)
        await s.commit()
    return engine, m


class _FakeTask:
    def __init__(self, file_id):
        self.file_id = file_id
        self.phase = "PHASE_TYPE_COMPLETE"


async def _harness(monkeypatch, m, *, move_fail=(), finalize_fail=()):
    # Neutralise the pre-loop machinery so the test targets the row loop.
    arch.state.enabled = True
    monkeypatch.setattr(arch.settings, "pikpak_username", "u")
    monkeypatch.setattr(arch, "_sweep_due", lambda: False)
    monkeypatch.setattr(arch, "_legacy_sweep_due", lambda: False)

    async def _noop(*a, **k):
        return 0

    monkeypatch.setattr(arch, "_finalize_retry_pass", _noop)
    monkeypatch.setattr(arch, "_reap_orphan_rows", _noop)

    # list_tasks → every seeded file_id is COMPLETE.
    async with m() as s:
        fids = [r.file_id for r in (await s.execute(select(OfflineTaskLog))).scalars()]

    async def fake_list_tasks(size=200):
        return [_FakeTask(f) for f in fids if f]

    monkeypatch.setattr(arch.pikpak_service, "list_tasks", fake_list_tasks)

    async def fake_ad_shell(svc, fid):
        return False

    monkeypatch.setattr("app.services.finalize.wrapper_is_ad_shell", fake_ad_shell)
    monkeypatch.setattr(arch, "_resolve_archive_path",
                        lambda row: _aret(f"AVBT/S/Ser/{row.code}"))

    async def fake_folder_id(path):
        return "fid-" + path.rsplit("/", 1)[-1]

    monkeypatch.setattr(arch.pikpak_service, "folder_id", fake_folder_id)

    async def fake_move(ids, to):
        if ids and ids[0] in move_fail:
            raise RuntimeError("move failed")
        return {}

    monkeypatch.setattr(arch.pikpak_service, "move_files", fake_move)

    calls: list[str] = []

    async def fake_finalize(svc, code, *, folder_id=None):
        calls.append(code)
        return code not in finalize_fail

    monkeypatch.setattr("app.services.finalize.run_finalize", fake_finalize)

    async def _noop_refresh(codes, **k):
        return 0

    # presence_index / invalidate_result_caches are imported inside
    # archive_once from their source modules — patch them there.
    monkeypatch.setattr(
        "app.services.pikpak_presence.presence_index.refresh_codes",
        _noop_refresh,
    )
    monkeypatch.setattr("app.services.missing.invalidate_result_caches",
                        lambda: None)
    monkeypatch.setattr(arch.webhook_queue, "enqueue_nowait",
                        lambda *a, **k: None)
    return calls


def _aret(v):
    async def _c(*a, **k):
        return v
    return _c()


def _mkrow(code, fid):
    return OfflineTaskLog(
        code=code, magnet="m", btih="", task_id="t-" + fid, file_id=fid,
        name="", phase="", message="", archived=False, finalized=False,
        created_at=datetime.utcnow() - timedelta(hours=1),
    )


async def test_archive_dedups_finalize_per_code(tmp_path, monkeypatch):
    # Two rows, same code, different file_ids (a re-download) → finalize once.
    engine, m = await _archive_db(tmp_path, monkeypatch,
                                  [_mkrow("DUP-1", "f1"), _mkrow("DUP-1", "f2")])
    calls = await _harness(monkeypatch, m)
    moved = await arch.archive_once()
    assert moved == 2                       # both files moved
    assert calls.count("DUP-1") == 1        # finalize ran once for the code
    async with m() as s:
        rows = (await s.execute(select(OfflineTaskLog))).scalars().all()
        assert all(r.archived and r.finalized for r in rows)
    await engine.dispose()


async def test_archive_does_not_finalize_unmoved_row(tmp_path, monkeypatch):
    # Same code, two rows; one move fails → only the moved row is finalized.
    engine, m = await _archive_db(tmp_path, monkeypatch,
                                  [_mkrow("DUP-2", "ok"), _mkrow("DUP-2", "bad")])
    await _harness(monkeypatch, m, move_fail={"bad"})
    await arch.archive_once()
    async with m() as s:
        by_fid = {r.file_id: r for r in
                  (await s.execute(select(OfflineTaskLog))).scalars()}
        assert by_fid["ok"].archived and by_fid["ok"].finalized
        assert not by_fid["bad"].archived and not by_fid["bad"].finalized
    await engine.dispose()


async def test_archive_isolates_finalize_failure(tmp_path, monkeypatch):
    # One code's finalize fails → the other still finalizes, batch commits.
    engine, m = await _archive_db(tmp_path, monkeypatch,
                                  [_mkrow("A-9", "fa"), _mkrow("B-9", "fb")])
    await _harness(monkeypatch, m, finalize_fail={"A-9"})
    await arch.archive_once()
    async with m() as s:
        by_code = {r.code: r for r in
                   (await s.execute(select(OfflineTaskLog))).scalars()}
        assert by_code["A-9"].archived and not by_code["A-9"].finalized
        assert by_code["B-9"].archived and by_code["B-9"].finalized
    await engine.dispose()


async def test_archive_once_end_to_end_at_finalize_concurrency_two(tmp_path, monkeypatch):
    # Full archive_once, 4 distinct codes, concurrency knob raised to 2:
    # must reach the same end-state as serial (all archived + finalized,
    # exactly one finalize call per code) while actually overlapping two
    # finalizes in flight — proving the concurrency knob is really wired
    # into the archive_once loop, not just the isolated _run_finalize_batch
    # unit above.
    engine, m = await _archive_db(tmp_path, monkeypatch, [
        _mkrow("PAR-1", "f1"), _mkrow("PAR-2", "f2"),
        _mkrow("PAR-3", "f3"), _mkrow("PAR-4", "f4"),
    ])
    await _harness(monkeypatch, m)
    monkeypatch.setattr(arch.settings, "archive_finalize_concurrency", 2)

    calls: list[str] = []
    active = 0
    peak = 0

    async def fake_finalize_overlap(svc, code, *, folder_id=None):
        nonlocal active, peak
        calls.append(code)
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)     # yield so overlap is possible/observable
        active -= 1
        return True

    # Re-patch over _harness's own run_finalize stub to add overlap tracking.
    monkeypatch.setattr("app.services.finalize.run_finalize", fake_finalize_overlap)

    moved = await arch.archive_once()

    assert moved == 4
    assert sorted(calls) == ["PAR-1", "PAR-2", "PAR-3", "PAR-4"]  # once each
    assert peak >= 2            # parallelism actually engaged
    assert peak <= 2            # and stayed within the concurrency=2 bound
    async with m() as s:
        rows = (await s.execute(select(OfflineTaskLog))).scalars().all()
        assert all(r.archived and r.finalized for r in rows)
    await engine.dispose()


async def test_archive_skips_list_tasks_when_only_abandoned_pending(
    tmp_path, monkeypatch
):
    # A dead-lettered stuck-"Saving" row keeps a stale nonempty file_id
    # (#203); it must not hold the pending peek open — that would burn a
    # PikPak list_tasks round-trip every 60s pass forever for rows nothing
    # will ever match.
    engine, m = await _archive_db(
        tmp_path, monkeypatch, [_mkrow("DEAD-9", "stale-fid")]
    )
    async with m() as s:
        row = (await s.execute(select(OfflineTaskLog))).scalars().one()
        row.abandoned = True
        await s.commit()
    arch.state.enabled = True
    monkeypatch.setattr(arch.settings, "pikpak_username", "u")
    monkeypatch.setattr(arch, "_sweep_due", lambda: False)
    monkeypatch.setattr(arch, "_legacy_sweep_due", lambda: False)

    async def _noop(*a, **k):
        return 0

    monkeypatch.setattr(arch, "_finalize_retry_pass", _noop)
    monkeypatch.setattr(arch, "_reap_orphan_rows", _noop)
    called = {"n": 0}

    async def fake_list_tasks(size=200):
        called["n"] += 1
        return []

    monkeypatch.setattr(arch.pikpak_service, "list_tasks", fake_list_tasks)
    assert await arch.archive_once() == 0
    assert called["n"] == 0   # pending peek short-circuits before list_tasks
    await engine.dispose()


async def test_archive_move_stamps_wrapper_settle_gate(tmp_path, monkeypatch):
    # The archive pass moves the wrapper into the series folder; the
    # wrapper's own listing is optimistic right after (#140), so the
    # move must stamp it — finalize's empty-shell trash keys off this.
    engine, m = await _archive_db(tmp_path, monkeypatch,
                                  [_mkrow("STMP-1", "f9")])
    await _harness(monkeypatch, m)
    stamps: list[str] = []
    monkeypatch.setattr(arch.pikpak_service, "record_move_source",
                        lambda sid: stamps.append(sid))
    await arch.archive_once()
    assert "f9" in stamps
    await engine.dispose()
