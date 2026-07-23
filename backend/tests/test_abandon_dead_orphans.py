"""Dead-letter genuinely-dead orphan rows so the finalize retry pass
stops re-listing them every ~10 min for the 7-day reap window.

A row is abandoned only when the code has nothing on PikPak
(_orphan_has_nothing_landed), the task is gone from PikPak, and it is
older than the 24h grace. The row's ``archived`` flag is deliberately NOT
part of that gate: a sweep-archived row whose files later evaporated is
equally dead, and gating on it stranded those rows permanently (neither
abandonable nor closeable). This covers both
the file_id-empty Collecting orphan AND the file_id-nonempty stuck-
"Saving" orphan (PikPak assigned a file_id but the download died) — the
per-row nothing-landed check, not the file_id, decides dead-vs-live.
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
import app.services.pikpak_presence as pp
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

    async def not_flat(code, **kw):
        return False

    async def _noop_refresh(codes, **kw):
        return 0

    async def _nothing(code, **kw):
        return True

    monkeypatch.setattr(archiver, "_active_task_ids", no_active)
    monkeypatch.setattr(archiver, "_already_flattened", not_flat)
    monkeypatch.setattr(archiver, "_orphan_has_nothing_landed", _nothing)
    monkeypatch.setattr(pp.presence_index, "refresh_codes", _noop_refresh)
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


def _stub_presence(monkeypatch, mapping):
    """Presence index without PikPak: ``mapping`` is code -> [paths]."""
    async def _get():
        return None

    monkeypatch.setattr(pp.presence_index, "get", _get)
    monkeypatch.setattr(
        pp.presence_index, "paths_for", lambda code: mapping.get(code, [])
    )


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
    async def yes_flat(code, **kw):
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


async def test_stuck_saving_orphan_with_file_id_is_abandoned(maker):
    # A stuck-"Saving" orphan keeps a stale file_id but the task is gone
    # and nothing landed — the file_id is not evidence of a real file, so
    # it is dead-lettered just like the file_id-empty Collecting orphan.
    # (Was previously left to churn the finalize retry pass forever; live
    # 2026-07-18 PA0-010/SNOS-257/GDTM-203.)
    async with maker() as s:
        s.add(_row(code="SAVING-001", file_id="f-123", message="Saving"))
        await s.commit()
    await archiver._reap_orphan_rows()
    async with maker() as s:
        row = (await s.execute(
            select(OfflineTaskLog).where(OfflineTaskLog.code == "SAVING-001")
        )).scalar_one()
        assert row.abandoned is True           # file_id no longer a shield
        assert row.finalized is False


async def test_stuck_saving_orphan_kept_when_something_landed(maker, monkeypatch):
    # file_id-nonempty, but a per-code folder / wrapper still exists → the
    # nothing-landed gate (not the file_id) keeps it out of abandon.
    async def _something(code, **kw):
        return False

    monkeypatch.setattr(archiver, "_orphan_has_nothing_landed", _something)
    async with maker() as s:
        s.add(_row(code="SAVING-002", file_id="f-456", message="Saving"))
        await s.commit()
    await archiver._reap_orphan_rows()
    async with maker() as s:
        row = (await s.execute(
            select(OfflineTaskLog).where(OfflineTaskLog.code == "SAVING-002")
        )).scalar_one()
        assert row.abandoned is False          # needs finalize, keep it


async def test_abandoned_row_excluded_from_retry_and_reap(maker):
    async with maker() as s:
        s.add(_row(code="GONE-001", abandoned=True))
        await s.commit()
    # Neither pass should select an already-abandoned row.
    n_reap = await archiver._reap_orphan_rows()
    n_retry = await archiver._finalize_retry_pass()
    assert n_reap == 0
    assert n_retry == 0


async def test_superseded_row_excluded_from_retry_and_reap(maker):
    async with maker() as s:
        s.add(_row(code="FOSSIL-001", superseded=True))
        await s.commit()
    # Neither pass should select an already-superseded row — the fossil
    # reconcile endpoint owns these now, not the live archiver loop.
    n_reap = await archiver._reap_orphan_rows()
    n_retry = await archiver._finalize_retry_pass()
    assert n_reap == 0
    assert n_retry == 0


async def test_already_flattened_strict_raises_on_error(monkeypatch):
    # strict=True must surface a check error; default must swallow to False.
    # Stub _resolve_archive_path_by_code (established pattern, see
    # test_finalize.py::test_flattened_check_requires_missing_folder) so
    # the check reaches lookup_folder_id without a real DB/network hop.
    async def fake_resolve(code):
        return "AVBT/製作商/S/系/ERR-001"

    async def boom(path):
        raise RuntimeError("pikpak throttled")

    monkeypatch.setattr(archiver, "_resolve_archive_path_by_code", fake_resolve)
    monkeypatch.setattr(archiver.pikpak_service, "lookup_folder_id", boom)
    assert await archiver._already_flattened("ERR-001") is False
    with pytest.raises(RuntimeError):
        await archiver._already_flattened("ERR-001", strict=True)


async def test_reaper_does_not_abandon_when_check_errors(maker, monkeypatch):
    # A transient flattened-check error must skip the row, never abandon it.
    async def boom(code, **kw):
        raise RuntimeError("pikpak throttled")

    monkeypatch.setattr(archiver, "_already_flattened", boom)
    async with maker() as s:
        s.add(_row(code="ERRDEAD-001"))
        await s.commit()
    await archiver._reap_orphan_rows()
    async with maker() as s:
        row = (await s.execute(
            select(OfflineTaskLog).where(OfflineTaskLog.code == "ERRDEAD-001")
        )).scalar_one()
        assert row.abandoned is False   # errored check → skipped, not abandoned


async def test_reaper_does_not_abandon_when_something_landed(maker, monkeypatch):
    # not flattened, but a per-code folder / task-wrapper exists → NOT dead.
    async def _something(code, **kw):
        return False

    monkeypatch.setattr(archiver, "_orphan_has_nothing_landed", _something)
    async with maker() as s:
        s.add(_row(code="LANDED-001"))
        await s.commit()
    await archiver._reap_orphan_rows()
    async with maker() as s:
        row = (await s.execute(
            select(OfflineTaskLog).where(OfflineTaskLog.code == "LANDED-001")
        )).scalar_one()
        assert row.abandoned is False   # needs finalize, must keep retrying


async def test_orphan_has_nothing_landed_real_paths(monkeypatch):
    # Drive the real function: a per-code folder → not nothing (False);
    # a check error → False by default, raises under strict.
    async def resolve(code):
        return "AVBT/製作商/S/Ser/" + code

    monkeypatch.setattr(archiver, "_resolve_archive_path_by_code", resolve)

    async def has_folder(path):
        return "fid-123"

    monkeypatch.setattr(archiver.pikpak_service, "lookup_folder_id", has_folder)
    _stub_presence(monkeypatch, {})
    assert await archiver._orphan_has_nothing_landed("PERCODE-001") is False

    async def boom(path):
        raise RuntimeError("pikpak throttled")

    monkeypatch.setattr(archiver.pikpak_service, "lookup_folder_id", boom)
    assert await archiver._orphan_has_nothing_landed("ERR-002") is False
    with pytest.raises(RuntimeError):
        await archiver._orphan_has_nothing_landed("ERR-002", strict=True)


async def test_reaper_abandons_via_real_predicate(tmp_path, monkeypatch):
    # End-to-end: drive the REAL _already_flattened AND
    # _orphan_has_nothing_landed (both consult the same stubbed lookups)
    # through the reaper — no per-code folder, nothing anywhere → abandoned.
    # Closes the gap where only the mocked wiring / the predicate's False
    # paths were covered, never the real abandon-triggering True path.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(archiver, "SessionLocal", m)
    monkeypatch.setattr(archiver, "_reap_attempts", {})

    async def no_active():
        return set()

    async def _noop_refresh(codes, **kw):
        return 0

    async def resolve(code):
        return "AVBT/製作商/S/Ser/" + code

    async def no_folder(path):
        return None

    async def no_bt_folders(svc, code):
        return []

    async def no_files(code):
        return {"ok": False}

    monkeypatch.setattr(archiver, "_active_task_ids", no_active)
    monkeypatch.setattr(pp.presence_index, "refresh_codes", _noop_refresh)
    monkeypatch.setattr(archiver, "_resolve_archive_path_by_code", resolve)
    monkeypatch.setattr(archiver.pikpak_service, "lookup_folder_id", no_folder)
    # presence_code_folders / files_for_code are imported inside the checks
    # from their source modules — patch them there.
    monkeypatch.setattr(
        "app.services.finalize.presence_code_folders", no_bt_folders
    )
    monkeypatch.setattr("app.services.video_count.files_for_code", no_files)
    _stub_presence(monkeypatch, {})

    async with m() as s:
        s.add(_row(code="REALDEAD-001"))
        await s.commit()
    await archiver._reap_orphan_rows()
    async with m() as s:
        row = (await s.execute(
            select(OfflineTaskLog).where(OfflineTaskLog.code == "REALDEAD-001")
        )).scalar_one()
        assert row.abandoned is True     # real predicate → real abandon
        assert row.finalized is False
    await engine.dispose()


async def test_archived_orphan_with_nothing_landed_is_abandoned(maker):
    # The limbo the reaper used to leave behind: the sweep stamped
    # archived (it moved a wrapper), the download then died and the task
    # vanished, so nothing is flattened and nothing is left on PikPak.
    # Abandon used to be gated on ``not row.archived`` and close on
    # _already_flattened, so neither fired and the row stayed pending
    # forever (live 2026-07-23: TRE-076, AP-491, STAR-264, …).
    async with maker() as s:
        s.add(_row(
            code="ZOMBIE-001",
            archived=True,
            archived_at=datetime.utcnow() - timedelta(hours=48),
            created_at=datetime.utcnow() - timedelta(hours=72),
        ))
        await s.commit()
    await archiver._reap_orphan_rows()
    async with maker() as s:
        row = (await s.execute(
            select(OfflineTaskLog).where(OfflineTaskLog.code == "ZOMBIE-001")
        )).scalar_one()
        assert row.abandoned is True
        assert row.finalized is False


async def test_archived_orphan_inside_retry_window_is_left(maker):
    # Still inside the finalize retry window → that pass owns the row;
    # the reaper must not select (nor abandon) it.
    async with maker() as s:
        s.add(_row(
            code="FRESHARCH-001",
            archived=True,
            archived_at=datetime.utcnow() - timedelta(hours=2),
            created_at=datetime.utcnow() - timedelta(hours=30),
        ))
        await s.commit()
    await archiver._reap_orphan_rows()
    async with maker() as s:
        row = (await s.execute(
            select(OfflineTaskLog).where(OfflineTaskLog.code == "FRESHARCH-001")
        )).scalar_one()
        assert row.abandoned is False


async def test_container_only_code_is_not_abandoned(monkeypatch):
    # A code whose only landing is a disc image: files_for_code resolves
    # PLAYABLE files only, so it reads empty — but 23GB really is on
    # PikPak and #173's container-swap still owns the code. Presence
    # knows the path, so "nothing landed" must be False.
    async def resolve(code):
        return "AVBT/製作商/S/Ser/" + code

    async def no_folder(path):
        return None

    async def no_bt_folders(svc, code):
        return []

    async def no_files(code):
        return {"ok": False}

    monkeypatch.setattr(archiver, "_resolve_archive_path_by_code", resolve)
    monkeypatch.setattr(archiver.pikpak_service, "lookup_folder_id", no_folder)
    monkeypatch.setattr(
        "app.services.finalize.presence_code_folders", no_bt_folders
    )
    monkeypatch.setattr("app.services.video_count.files_for_code", no_files)

    _stub_presence(monkeypatch, {"IPTD-770": ["AVBT/製作商/S/Ser/IPTD-770.iso"]})
    assert await archiver._orphan_has_nothing_landed("IPTD-770") is False
    # …while a code presence has never heard of stays abandon-eligible.
    assert await archiver._orphan_has_nothing_landed("TRE-076") is True
