"""Post-download finalize: keep only canonical videos in the 番號 folder,
permanently purge junk, trash resolution dups. Covers the pure planner
(:func:`build_finalize_plan`) and the streaming executor."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.archiver as arch
import app.services.finalize as fin
from app.database import Base
from app.models import OfflineTaskLog
from app.services.finalize import (
    FinalizePlan,
    build_finalize_plan,
    finalize_code_folder_stream,
    run_finalize,
)

MB = 1024 * 1024
GB = 1024 * MB


def _folder(name, id):
    return SimpleNamespace(name=name, id=id, kind="drive#folder", size=None)


def _file(name, id, size_mb=600):
    return SimpleNamespace(name=name, id=id, kind="drive#file", size=size_mb * MB)


# ---------------------------------------------------------------------------
# build_finalize_plan — pure, no I/O
# ---------------------------------------------------------------------------

def test_single_video_plus_junk_and_sample_folder():
    v = _file("[88K.ME]MIDV-001.mp4", "v", 2048)
    junk = [_file("下載說明.txt", "t", 0), _file("網址.url", "u", 0),
            _file("cover.jpg", "j", 1)]
    sample = _folder("Sample", "s")
    entries = [(v, "root"), (sample, "root")] + [(j, "root") for j in junk]
    plan = build_finalize_plan("MIDV-001", entries, "root")
    assert plan.keep == [(v, "MIDV-001.mp4")]
    assert {e.id for e in plan.purge_files} == {"t", "u", "j"}
    assert [f.id for f in plan.purge_folders] == ["s"]
    assert not plan.trash_files and not plan.move_to_root
    assert not plan.no_video and not plan.skipped_all_clean


def test_two_substantial_parts_get_underscore_names():
    a = _file("SDMM-053.mp4", "a", 900)
    b = _file("SDMM-053 (2).mp4", "b", 900)
    plan = build_finalize_plan("SDMM-053", [(a, "root"), (b, "root")], "root")
    assert sorted(t for _k, t in plan.keep) == ["SDMM-053_1.mp4", "SDMM-053_2.mp4"]
    assert not plan.trash_files and not plan.purge_files


def test_small_ad_clip_beside_big_keeper_is_purged():
    keeper = _file("MIDV-001.mp4", "k", 2048)
    ad = _file("最新famous_ad.mp4", "ad", 100)
    plan = build_finalize_plan("MIDV-001", [(keeper, "root"), (ad, "root")], "root")
    assert [k.id for k, _t in plan.keep] == ["k"]
    assert [e.id for e in plan.purge_files] == ["ad"]


def test_only_video_is_small_but_never_deleted():
    v = _file("MIDV-001.mp4", "v", 150)
    plan = build_finalize_plan("MIDV-001", [(v, "root")], "root")
    assert plan.keep == [(v, "MIDV-001.mp4")]
    assert not plan.purge_files and not plan.trash_files


def test_zero_videos_aborts_without_actions():
    entries = [(_file("readme.txt", "t", 0), "root"), (_folder("Sample", "s"), "root")]
    plan = build_finalize_plan("MIDV-001", entries, "root")
    assert plan.no_video
    assert plan == FinalizePlan(no_video=True)


def test_already_canonical_folder_is_all_clean():
    a = _file("SDMM-053_1.mp4", "a", 900)
    b = _file("SDMM-053_2.mp4", "b", 900)
    plan = build_finalize_plan("SDMM-053", [(a, "root"), (b, "root")], "root")
    assert plan.skipped_all_clean
    assert not plan.purge_files and not plan.purge_folders and not plan.move_to_root


def test_smaller_same_canonical_dup_goes_to_trash_not_purge():
    big = _file("MIDV-001.mp4", "big", 600)
    small = _file("MIDV-001 (2).mp4", "small", 400)  # ≥300MB → recoverable
    plan = build_finalize_plan("MIDV-001", [(big, "root"), (small, "root")], "root")
    assert [k.id for k, _t in plan.keep] == ["big"]
    assert [e.id for e in plan.trash_files] == ["small"]
    assert not plan.purge_files


def test_uncodeable_name_falls_back_to_code():
    v = _file("movie.mp4", "v", 2048)
    plan = build_finalize_plan("MIDV-001", [(v, "root")], "root")
    assert plan.keep == [(v, "MIDV-001.mp4")]


def test_nested_keeper_is_marked_for_move_to_root():
    wrap = _folder("MIDV-001@BT", "wrap")
    v = _file("MIDV-001.mp4", "v", 2048)
    plan = build_finalize_plan("MIDV-001", [(wrap, "root"), (v, "wrap")], "root")
    assert [k.id for k in plan.move_to_root] == ["v"]
    assert [f.id for f in plan.purge_folders] == ["wrap"]


# ---------------------------------------------------------------------------
# executor — FakeSvc records every mutator call
# ---------------------------------------------------------------------------

class FakeSvc:
    def __init__(self, graph, path_ids=None):
        self._graph = {k: list(v) for k, v in graph.items()}
        self._path_ids = dict(path_ids or {})
        self.moved = []
        self.renamed = []
        self.trashed = []
        self.purged = []

    async def list_all_files(self, parent_id, *, cap=5000):
        return list(self._graph.get(parent_id, [])), False

    async def lookup_folder_id(self, path):
        return self._path_ids.get(path)

    def _parent_of(self, node_id):
        for pid, kids in self._graph.items():
            for n in kids:
                if n.id == node_id:
                    return pid, n
        return None, None

    async def rename_file(self, fid, new_name):
        self.renamed.append((fid, new_name))
        _pid, node = self._parent_of(fid)
        if node is not None:
            node.name = new_name
        return {}

    async def move_files(self, ids, parent_id):
        self.moved.append((list(ids), parent_id))
        for nid in ids:
            pid, node = self._parent_of(nid)
            if pid is not None:
                self._graph[pid] = [n for n in self._graph[pid] if n.id != nid]
                self._graph.setdefault(parent_id, []).append(node)
        return {}

    def _remove(self, ids):
        for nid in ids:
            pid, _n = self._parent_of(nid)
            if pid is not None:
                self._graph[pid] = [n for n in self._graph[pid] if n.id != nid]
            self._graph.pop(nid, None)

    async def trash_files(self, ids):
        self.trashed.extend(ids)
        self._remove(ids)
        return {}

    async def delete_forever(self, ids):
        self.purged.extend(ids)
        self._remove(ids)
        return {}

    async def confirm_arrivals(self, parent_id, file_ids, **_kw):
        # Mirror the real semantics without the polling: arrival = the
        # id is listed under the destination right now.
        return set(file_ids) <= {n.id for n in self._graph.get(parent_id, [])}

    # Settle gate: fake moves are instantaneous, so tests default to an
    # open gate; set ``settled = False`` to simulate an in-flight move.
    settled = True

    def record_move_source(self, source_id):
        pass

    def move_settled(self, source_id):
        return self.settled


async def _collect(svc, code, folder_id, *, dry_run):
    return [e async for e in finalize_code_folder_stream(
        svc, code, folder_id=folder_id, dry_run=dry_run)]


def _wrapper_graph():
    """番號夾 root → wrapper(video + junk + Sample/screens)."""
    return {
        "root": [_folder("MIDV-001@nyaa", "wrap")],
        "wrap": [
            _file("[88K.ME]MIDV-001.mp4", "vid", 2048),
            _file("最新網址.txt", "txt", 0),
            _file("廣告.mp4", "ad", 80),
            _folder("Sample", "smp"),
        ],
        "smp": [_file("screen1.jpg", "s1", 1)],
    }


async def test_executor_flattens_renames_and_purges():
    svc = FakeSvc(_wrapper_graph())
    events = await _collect(svc, "MIDV-001", "root", dry_run=False)
    done = events[-1]
    assert done["type"] == "done"
    r = done["result"]
    assert r["errors"] == 0 and not r["dry_run"]
    assert r["kept"] == 1 and r["renamed"] == 1 and r["moved"] == 1
    # keeper renamed + evacuated to root
    assert svc.renamed == [("vid", "MIDV-001.mp4")]
    assert svc.moved == [(["vid"], "root")]
    # junk FILES permanently gone; emptied FOLDERS only to trash — a
    # slow disc can materialise inside hours later, invisible to every
    # re-list, and must stay recoverable (live losses: DVDMS-172_2,
    # SDMU-845_6).
    assert set(svc.purged) == {"txt", "ad", "s1"}
    assert set(svc.trashed) == {"smp", "wrap"}
    # root now holds exactly the canonical video
    assert [n.name for n in svc._graph["root"]] == ["MIDV-001.mp4"]


async def test_executor_reports_removed_ids_for_presence_refresh():
    """finalize must name what it deleted.

    PikPak keeps listing a just-trashed wrapper, so a presence refresh
    that re-reads the folder right after finalize sees the dead entry,
    computes the pre-finalize paths, and skips its write as a no-op —
    stranding the phantom folder in the index until an unrelated full
    walk clears it. The ids travel with the summary so the refresh can
    ignore them instead.
    """
    svc = FakeSvc(_wrapper_graph())
    events = await _collect(svc, "MIDV-001", "root", dry_run=False)
    r = events[-1]["result"]
    # Everything actually removed — the wrapper included — is reported.
    assert set(r["gone_ids"]) == set(svc.purged) | set(svc.trashed)
    assert "wrap" in r["gone_ids"]


async def test_executor_dry_run_reports_no_removed_ids():
    svc = FakeSvc(_wrapper_graph())
    events = await _collect(svc, "MIDV-001", "root", dry_run=True)
    assert events[-1]["result"]["gone_ids"] == []


async def test_executor_dry_run_touches_nothing():
    svc = FakeSvc(_wrapper_graph())
    events = await _collect(svc, "MIDV-001", "root", dry_run=True)
    assert events[-1]["result"]["dry_run"] is True
    assert not svc.moved and not svc.renamed and not svc.trashed and not svc.purged


async def test_executor_rerun_is_noop():
    svc = FakeSvc(_wrapper_graph())
    await _collect(svc, "MIDV-001", "root", dry_run=False)
    for calls in (svc.moved, svc.renamed, svc.purged, svc.trashed):
        calls.clear()
    events = await _collect(svc, "MIDV-001", "root", dry_run=False)
    assert events[-1]["result"]["skipped"] == 1  # all-clean fast path
    assert not svc.moved and not svc.renamed and not svc.purged and not svc.trashed


async def test_keeper_move_failure_aborts_all_deletion():
    class BrokenMove(FakeSvc):
        async def move_files(self, ids, parent_id):
            raise RuntimeError("boom")

    svc = BrokenMove(_wrapper_graph())
    events = await _collect(svc, "MIDV-001", "root", dry_run=False)
    assert any(e["type"] == "error" for e in events)
    assert not svc.purged and not svc.trashed  # wrapper + junk untouched


async def test_folder_with_unplanned_leftover_is_skipped():
    svc = FakeSvc(_wrapper_graph())
    # A file that appears mid-run (not in the plan) must protect its folder.
    orig = svc.delete_forever

    async def sneaky_delete(ids):
        svc._graph["wrap"].append(_file("late-arrival.mkv", "late", 700))
        svc.delete_forever = orig
        return await orig(ids)

    svc.delete_forever = sneaky_delete
    events = await _collect(svc, "MIDV-001", "root", dry_run=False)
    skips = [e for e in events if e.get("action") == "skip" and e.get("kind") == "folder"]
    assert skips and skips[0]["reason"] == "not_empty"
    assert "wrap" not in svc.purged and "wrap" not in svc.trashed


async def test_no_video_aborts_and_run_finalize_returns_none():
    svc = FakeSvc({"root": [_file("readme.txt", "t", 0)]})
    events = await _collect(svc, "MIDV-001", "root", dry_run=False)
    assert events[0]["type"] == "warn"
    assert events[-1]["result"]["no_video"] is True
    assert not svc.purged and not svc.trashed
    assert await run_finalize(svc, "MIDV-001", folder_id="root") is None


async def test_run_finalize_success_returns_summary():
    svc = FakeSvc(_wrapper_graph())
    summary = await run_finalize(svc, "MIDV-001", folder_id="root")
    assert summary and summary["errors"] == 0 and summary["kept"] == 1


# ---------------------------------------------------------------------------
# archiver retry pass
# ---------------------------------------------------------------------------

async def _retry_db(tmp_path, monkeypatch, rows):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/f.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(arch, "SessionLocal", maker)
    # The retry pass force-refreshes the presence index before touching
    # any row — stub it so unit tests never walk the real drive.
    from app.services.pikpak_presence import presence_index

    async def fake_get(*, force=False):
        return set()

    async def fake_refresh(codes):
        return 0

    monkeypatch.setattr(presence_index, "get", fake_get)
    # The retry pass refreshes each pending code's folder — unstubbed
    # this reaches the real PikPak/JavBus clients (a live run once took
    # the suite from 13s to 20min).
    monkeypatch.setattr(presence_index, "refresh_codes", fake_refresh)
    # Module-level cooldowns must not leak between tests (fresh DBs
    # restart row ids at 1, so stale entries would shadow new rows).
    arch._finalize_attempts.clear()
    arch._reap_attempts.clear()
    async with maker() as s:
        s.add_all(rows)
        await s.commit()
    return engine, maker


async def test_finalize_retry_pass_marks_row(tmp_path, monkeypatch):
    now = datetime.utcnow()
    engine, maker = await _retry_db(tmp_path, monkeypatch, [
        OfflineTaskLog(code="MIDV-001", magnet="m", archived=True,
                       archived_at=now, finalized=False,
                       created_at=now - timedelta(hours=1)),
        OfflineTaskLog(code="OLD-999", magnet="m", archived=True,
                       archived_at=now - timedelta(hours=48), finalized=False,
                       created_at=now - timedelta(hours=49)),
    ])

    calls = []

    async def fake_run_finalize(svc, code, *, folder_id=None):
        calls.append(code)
        return {"errors": 0}

    async def no_active():
        return set()

    monkeypatch.setattr(fin, "run_finalize", fake_run_finalize)
    monkeypatch.setattr(arch, "_active_task_ids", no_active)
    done = await arch._finalize_retry_pass()
    assert done == 1
    assert calls == ["MIDV-001"]  # 48h-old row is outside the window

    async with maker() as s:
        rows = {r.code: r for r in (await s.execute(select(OfflineTaskLog))).scalars()}
    assert rows["MIDV-001"].finalized is True
    assert rows["OLD-999"].finalized is False
    await engine.dispose()


async def test_finalize_retry_skips_still_downloading_task(tmp_path, monkeypatch):
    """The sweep can flag a wrapper archived while its offline task is
    still RUNNING — finalize (permanent deletes) must wait it out."""
    now = datetime.utcnow()
    engine, maker = await _retry_db(tmp_path, monkeypatch, [
        OfflineTaskLog(code="MIDV-001", magnet="m", task_id="t-run",
                       archived=True, archived_at=now, finalized=False,
                       created_at=now - timedelta(hours=1)),
        OfflineTaskLog(code="MIDV-002", magnet="m", task_id="t-done",
                       archived=True, archived_at=now, finalized=False,
                       created_at=now - timedelta(hours=1)),
    ])

    calls = []

    async def fake_run_finalize(svc, code, *, folder_id=None):
        calls.append(code)
        return {"errors": 0}

    async def active():
        return {"t-run"}

    monkeypatch.setattr(fin, "run_finalize", fake_run_finalize)
    monkeypatch.setattr(arch, "_active_task_ids", active)
    done = await arch._finalize_retry_pass()
    assert done == 1
    assert calls == ["MIDV-002"]  # the RUNNING task's row is deferred

    async with maker() as s:
        rows = {r.code: r for r in (await s.execute(select(OfflineTaskLog))).scalars()}
    assert rows["MIDV-001"].finalized is False
    assert rows["MIDV-002"].finalized is True
    await engine.dispose()


async def test_finalize_retry_fails_closed_without_task_list(tmp_path, monkeypatch):
    now = datetime.utcnow()
    engine, _maker = await _retry_db(tmp_path, monkeypatch, [
        OfflineTaskLog(code="MIDV-001", magnet="m", task_id="t1",
                       archived=True, archived_at=now, finalized=False,
                       created_at=now - timedelta(hours=1)),
    ])

    async def fake_run_finalize(svc, code, *, folder_id=None):
        raise AssertionError("must not finalize when task list is unknown")

    async def boom():
        raise arch.PikPakError("list_tasks unavailable: down")

    monkeypatch.setattr(fin, "run_finalize", fake_run_finalize)
    monkeypatch.setattr(arch, "_active_task_ids", boom)
    assert await arch._finalize_retry_pass() == 0
    await engine.dispose()

async def test_skipped_deep_folder_protects_its_ancestors():
    """A video buried below MAX_DEPTH is invisible at plan time; the
    runtime re-list correctly skips its folder — and that skip must
    propagate upward so no ancestor is purged around the survivor."""
    svc = FakeSvc({
        "root": [_folder("MIDV-001@nyaa", "wrap")],
        "wrap": [_file("[88K.ME]MIDV-001.mp4", "vid", 2048), _folder("extras", "ex")],
        "ex": [_folder("bonus", "bn")],                # level 3: seen, unexplored
        "bn": [_file("bonus.mkv", "deep", 700)],       # level 4: invisible to the plan
    })
    events = await _collect(svc, "MIDV-001", "root", dry_run=False)
    assert events[-1]["result"]["errors"] == 0
    # bn skipped (unplanned child), and the skip cascades: ex and wrap
    # both survive instead of being purged around it.
    skipped = {e["source"] for e in events
               if e.get("action") == "skip" and e.get("kind") == "folder"}
    assert skipped == {"bonus", "extras", "MIDV-001@nyaa"}
    assert not any(f in svc.purged or f in svc.trashed
                   for f in ("bn", "ex", "wrap"))
    # The deep video is untouched.
    assert any(n.id == "deep" for n in svc._graph["bn"])


async def test_folder_removal_failure_protects_its_ancestors():
    class FlakyTrash(FakeSvc):
        async def trash_files(self, ids):
            if "smp" in ids:
                raise RuntimeError("boom")
            return await super().trash_files(ids)

    svc = FlakyTrash(_wrapper_graph())
    events = await _collect(svc, "MIDV-001", "root", dry_run=False)
    errs = [e for e in events if e.get("action") == "error" and e.get("kind") == "folder"]
    assert errs and errs[0]["source"] == "Sample"
    # Sample failed to go → wrap must NOT be removed around it.
    assert "wrap" not in svc.purged and "wrap" not in svc.trashed

async def test_finalize_retry_marks_flattened_layout_row(tmp_path, monkeypatch):
    """Sweep-archived rows have no per-code folder — the video sits in
    the 系列 folder. run_finalize misses, but the row must still leave
    the retry queue via the flattened-layout check."""
    now = datetime.utcnow()
    engine, maker = await _retry_db(tmp_path, monkeypatch, [
        OfflineTaskLog(code="RCTD-740", magnet="m", task_id="t1",
                       archived=True, archived_at=now, finalized=False,
                       created_at=now - timedelta(hours=1)),
    ])

    async def fake_run_finalize(svc, code, *, folder_id=None):
        return None  # 找不到歸檔資料夾

    async def no_active():
        return set()

    async def flattened(code):
        assert code == "RCTD-740"
        return True

    monkeypatch.setattr(fin, "run_finalize", fake_run_finalize)
    monkeypatch.setattr(arch, "_active_task_ids", no_active)
    monkeypatch.setattr(arch, "_already_flattened", flattened)
    assert await arch._finalize_retry_pass() == 1
    async with maker() as s:
        row = (await s.execute(select(OfflineTaskLog))).scalars().one()
    assert row.finalized is True
    await engine.dispose()


async def test_flattened_check_requires_missing_folder(monkeypatch):
    """A real finalize failure (folder exists) must NOT be masked by the
    flattened-layout check."""
    async def fake_resolve(code):
        return "AVBT/製作商/S/系/CODE-1"

    async def folder_exists(path):
        return "some-id"

    monkeypatch.setattr(arch, "_resolve_archive_path_by_code", fake_resolve)
    monkeypatch.setattr(arch.pikpak_service, "lookup_folder_id", folder_exists)
    assert await arch._already_flattened("CODE-1") is False


def _running_file(name, id, size_mb=100):
    f = _file(name, id, size_mb)
    f.phase = "PHASE_TYPE_RUNNING"
    return f


async def test_transferring_file_aborts_finalize():
    """A half-transferred second disc looks like a sub-300MB ad clip —
    the per-file phase must abort the whole run before any deletion."""
    svc = FakeSvc({
        "root": [_folder("IDBD-924@bt", "wrap")],
        "wrap": [
            _file("idbd-924-1.mp4", "d1", 10000),
            _running_file("idbd-924-2.mp4", "d2", 120),  # still downloading
            _file("ads.txt", "t", 0),
        ],
    })
    events = await _collect(svc, "IDBD-924", "root", dry_run=False)
    assert any(e["type"] == "error" and "傳輸中" in e["message"] for e in events)
    assert not svc.purged and not svc.trashed and not svc.moved and not svc.renamed


async def test_complete_phase_files_do_not_abort():
    g = _wrapper_graph()
    for n in g["wrap"]:
        n.phase = "PHASE_TYPE_COMPLETE"
    svc = FakeSvc(g)
    events = await _collect(svc, "MIDV-001", "root", dry_run=False)
    assert events[-1]["type"] == "done" and events[-1]["result"]["errors"] == 0


def test_plan_outlier_rip_goes_to_trash_not_part_slot():
    discs = [_file(f"sdmu-845cd{i}.mp4", f"c{i}", 4400) for i in range(1, 6)]
    old = _file("SDMU-845.mp4", "old", 1440)
    entries = [(e, "root") for e in discs + [old]]
    plan = build_finalize_plan("SDMU-845", entries, "root")
    targets = {k.id: t for k, t in plan.keep}
    assert "old" not in targets
    assert [e.id for e in plan.trash_files] == ["old"]
    assert targets["c5"] == "SDMU-845_5.mp4"


async def test_finalize_retry_waits_out_settle_grace(tmp_path, monkeypatch):
    now = datetime.utcnow()
    engine, maker = await _retry_db(tmp_path, monkeypatch, [
        OfflineTaskLog(code="MIDV-001", magnet="m", task_id="t1",
                       archived=True, archived_at=now, finalized=False,
                       created_at=now),  # just submitted — inside grace
    ])

    async def fake_run_finalize(svc, code, *, folder_id=None):
        raise AssertionError("must not finalize inside the settle grace")

    async def no_active():
        return set()

    monkeypatch.setattr(fin, "run_finalize", fake_run_finalize)
    monkeypatch.setattr(arch, "_active_task_ids", no_active)
    assert await arch._finalize_retry_pass() == 0
    await engine.dispose()


def test_recently_created_helper():
    from datetime import datetime, timedelta
    from types import SimpleNamespace as NS

    from app.services.offline_tasks import recently_created

    fresh = NS(created_time=(datetime.now(UTC)
                             - timedelta(minutes=3)).isoformat())
    old = NS(created_time=(datetime.now(UTC)
                           - timedelta(hours=1)).isoformat())
    none = NS(created_time=None)
    bad = NS(created_time="not-a-date")
    assert recently_created([old, fresh]) is True
    assert recently_created([old, none]) is False
    assert recently_created([bad]) is True  # unparseable → fail closed
    assert recently_created([]) is False


# ---------------------------------------------------------------------------
# presence fallback — the sweep moves wrappers wholesale, keeping BT names
# ---------------------------------------------------------------------------

def _patch_presence(monkeypatch, paths):
    from app.services.pikpak_presence import presence_index
    monkeypatch.setattr(presence_index, "paths_for", lambda code: list(paths))


async def test_stream_resolves_bt_named_wrapper_via_presence(monkeypatch):
    """Canonical path misses ([Thz.la]dvdms-129 ≠ DVDMS-129) — the stream
    must find the wrapper through presence, finalize it AND rename the
    folder itself to the canonical leaf."""
    wrapper_path = "AVBT/製作商/ディープス/MM便/[Thz.la]dvdms-129"
    svc = FakeSvc({
        "series": [_folder("[Thz.la]dvdms-129", "wrap")],
        "wrap": [
            _file("[Thz.la]dvdms-129cd1.mp4", "v1", 4000),
            _file("[Thz.la]dvdms-129cd2.mp4", "v2", 3000),
            _file("最新網址.txt", "txt", 0),
        ],
    }, path_ids={wrapper_path: "wrap"})

    async def fake_resolve(code):
        return "AVBT/製作商/ディープス/MM便/DVDMS-129"

    monkeypatch.setattr(arch, "_resolve_archive_path_by_code", fake_resolve)
    _patch_presence(monkeypatch, [wrapper_path])

    events = [e async for e in finalize_code_folder_stream(
        svc, "DVDMS-129", dry_run=False)]
    done = events[-1]
    assert done["type"] == "done" and done["result"]["errors"] == 0
    assert ("wrap", "DVDMS-129") in svc.renamed        # folder normalised
    assert ("v1", "DVDMS-129_1.mp4") in svc.renamed
    assert ("v2", "DVDMS-129_2.mp4") in svc.renamed
    assert "txt" in svc.purged


async def test_stream_ambiguous_presence_folders_abort(monkeypatch):
    """Two candidate per-code folders → refuse to guess, no mutations."""
    p1, p2 = "AVBT/A/[Thz]dvdms-129", "AVBT/B/dvdms-129"
    svc = FakeSvc({
        "a": [_folder("[Thz]dvdms-129", "w1")],
        "b": [_folder("dvdms-129", "w2")],
    }, path_ids={p1: "w1", p2: "w2"})

    async def fake_resolve(code):
        return "AVBT/X/DVDMS-129"

    monkeypatch.setattr(arch, "_resolve_archive_path_by_code", fake_resolve)
    _patch_presence(monkeypatch, [p1, p2])

    events = [e async for e in finalize_code_folder_stream(
        svc, "DVDMS-129", dry_run=False)]
    assert events[0]["type"] == "error"
    assert not svc.moved and not svc.renamed and not svc.purged and not svc.trashed


async def test_flattened_check_sees_bt_named_wrapper(monkeypatch):
    """A wrapper folder with a non-canonical name is still a per-code
    folder — _already_flattened must NOT stamp the row finalized."""
    wrapper_path = "AVBT/製作商/SOD/系/sdmm-053@bt"
    fake_svc = FakeSvc({}, path_ids={wrapper_path: "wrap"})

    async def fake_resolve(code):
        return "AVBT/製作商/SOD/系/SDMM-053"

    monkeypatch.setattr(arch, "_resolve_archive_path_by_code", fake_resolve)
    monkeypatch.setattr(arch, "pikpak_service", fake_svc)
    _patch_presence(monkeypatch, [wrapper_path])
    assert await arch._already_flattened("SDMM-053") is False


async def test_flattened_check_true_for_loose_video(monkeypatch):
    """The genuinely-flattened layout (loose CODE.ext in the series
    folder, no per-code folder anywhere) still counts as flattened."""
    loose = "AVBT/製作商/SOD/系/SDMM-053.mp4"
    fake_svc = FakeSvc({}, path_ids={})

    async def fake_resolve(code):
        return "AVBT/製作商/SOD/系/SDMM-053"

    async def fake_files(code):
        return {"ok": True, "files": [{"name": "SDMM-053.mp4"}]}

    import app.services.video_count as vc
    monkeypatch.setattr(arch, "_resolve_archive_path_by_code", fake_resolve)
    monkeypatch.setattr(arch, "pikpak_service", fake_svc)
    monkeypatch.setattr(vc, "files_for_code", fake_files)
    _patch_presence(monkeypatch, [loose])
    assert await arch._already_flattened("SDMM-053") is True


# ---------------------------------------------------------------------------
# separator-prefixed letter markers (SDMM-053_A / TRE-143-A) are disc parts
# ---------------------------------------------------------------------------

def test_letter_parts_with_separator_become_numeric():
    """4 substantial files CODE_A.._D are one boxset — rename to _1.._4
    in letter order (observed live on SDMM-053: they used to be four
    'distinct canonicals' and kept their letters forever)."""
    files = [_file(f"SDMM-053_{ch}.mp4", ch, 1800) for ch in "ABCD"]
    plan = build_finalize_plan(
        "SDMM-053", [(f, "root") for f in files], "root")
    assert not plan.skipped_all_clean
    targets = {k.id: t for k, t in plan.keep}
    assert targets == {"A": "SDMM-053_1.mp4", "B": "SDMM-053_2.mp4",
                       "C": "SDMM-053_3.mp4", "D": "SDMM-053_4.mp4"}


def test_dash_letter_parts_also_group():
    files = [_file(f"TRE-999-{ch}.mp4", ch, 2000) for ch in "AB"]
    plan = build_finalize_plan("TRE-999", [(f, "root") for f in files], "root")
    targets = {k.id: t for k, t in plan.keep}
    assert targets == {"A": "TRE-999_1.mp4", "B": "TRE-999_2.mp4"}


def test_lonely_separated_letter_is_stripped():
    f = _file("MIDV-001_A.mp4", "v", 2048)
    plan = build_finalize_plan("MIDV-001", [(f, "root")], "root")
    assert [(k.id, t) for k, t in plan.keep] == [("v", "MIDV-001.mp4")]


def test_codec_token_is_not_a_part_letter():
    """`x264`-style tokens must not be eaten as a variant letter — the
    lookahead requires the letter to stand alone."""
    from app.services.rename_plan import _canonical_video_name
    assert (_canonical_video_name("ABC-123 x264.mp4")
            != _canonical_video_name("ABC-123.mp4"))


# ---------------------------------------------------------------------------
# non-bracket BT prefixes + retry-pass presence freshness
# ---------------------------------------------------------------------------

def test_canonical_strips_non_bracket_site_prefixes():
    from app.services.rename_plan import _canonical_video_name
    assert _canonical_video_name("HD-DVDMS-475_1.mp4") == "DVDMS-475"
    assert _canonical_video_name("139_3XPLANET_TRE-016.mp4") == "TRE-016"
    # code mid-name is NOT collapsed — only trailing-code stems qualify
    assert _canonical_video_name("MIDV-001 making-of.mp4") != "MIDV-001"


def test_prefixed_parts_rename_to_bare_code():
    files = [_file("HD-DVDMS-475_1.mp4", "a", 3000),
             _file("HD-DVDMS-475_2.mp4", "b", 2700)]
    plan = build_finalize_plan(
        "DVDMS-475", [(f, "root") for f in files], "root")
    targets = {k.id: t for k, t in plan.keep}
    assert targets == {"a": "DVDMS-475_1.mp4", "b": "DVDMS-475_2.mp4"}


async def test_finalize_retry_fails_closed_without_presence(tmp_path, monkeypatch):
    """Stale presence wrongly stamped DVDMS-306 flattened — a pass that
    cannot refresh the index must not touch any row."""
    now = datetime.now(UTC).replace(tzinfo=None)
    engine, maker = await _retry_db(tmp_path, monkeypatch, [
        OfflineTaskLog(task_id="t1", code="DVDMS-306", name="x",
                       magnet="magnet:?xt=t1",
                       created_at=now - timedelta(hours=1),
                       archived=True, archived_at=now, finalized=False),
    ])

    async def fake_run_finalize(svc, code, *, folder_id=None):
        raise AssertionError("must not finalize when presence is stale")

    async def no_active():
        return set()

    from app.services.pikpak_presence import presence_index

    async def broken_get(*, force=False):
        raise RuntimeError("presence walk failed")

    monkeypatch.setattr(fin, "run_finalize", fake_run_finalize)
    monkeypatch.setattr(arch, "_active_task_ids", no_active)
    monkeypatch.setattr(presence_index, "get", broken_get)
    assert await arch._finalize_retry_pass() == 0
    async with maker() as s:
        row = (await s.execute(select(OfflineTaskLog))).scalars().one()
    assert row.finalized is False
    await engine.dispose()


# ---------------------------------------------------------------------------
# retry-pass hang protection + conditional presence force-refresh
# ---------------------------------------------------------------------------

async def test_finalize_retry_row_timeout_does_not_freeze_pass(tmp_path, monkeypatch):
    """One stuck PikPak mutation froze the whole archiver loop live —
    a hanging row must burn its own timeout and let the rest proceed."""
    import asyncio

    now = datetime.now(UTC).replace(tzinfo=None)
    engine, maker = await _retry_db(tmp_path, monkeypatch, [
        OfflineTaskLog(task_id="t1", code="AAA-001", name="a",
                       magnet="magnet:?xt=1",
                       created_at=now - timedelta(hours=1),
                       archived=True, archived_at=now, finalized=False),
        OfflineTaskLog(task_id="t2", code="BBB-002", name="b",
                       magnet="magnet:?xt=2",
                       created_at=now - timedelta(hours=1),
                       archived=True, archived_at=now, finalized=False),
    ])

    async def run(svc, code, *, folder_id=None):
        if code == "AAA-001":
            await asyncio.sleep(30)  # simulated hung mutation
        return {"errors": 0}

    async def no_active():
        return set()

    monkeypatch.setattr(fin, "run_finalize", run)
    monkeypatch.setattr(arch, "_active_task_ids", no_active)
    monkeypatch.setattr(arch, "_FINALIZE_ROW_TIMEOUT", 0.05)
    assert await arch._finalize_retry_pass() == 1
    async with maker() as s:
        rows = (await s.execute(select(OfflineTaskLog))).scalars().all()
    by_code = {r.code: r.finalized for r in rows}
    assert by_code == {"AAA-001": False, "BBB-002": True}
    await engine.dispose()


async def test_retry_pass_refreshes_only_this_pass_codes(tmp_path, monkeypatch):
    """The pass must refresh exactly the codes it is about to finalize —
    never the whole index (that walk is 10k+ codes / minutes and used to
    run on every stale pass)."""
    from app.services.pikpak_presence import presence_index

    now = datetime.now(UTC).replace(tzinfo=None)
    engine, _maker = await _retry_db(tmp_path, monkeypatch, [
        OfflineTaskLog(task_id="t1", code="CCC-003", name="c",
                       magnet="magnet:?xt=3",
                       created_at=now - timedelta(hours=1),
                       archived=True, archived_at=now - timedelta(minutes=10),
                       finalized=False),
        OfflineTaskLog(task_id="t2", code="CCC-004", name="d",
                       magnet="magnet:?xt=4",
                       created_at=now - timedelta(hours=2),
                       archived=True, archived_at=now - timedelta(minutes=20),
                       finalized=False),
    ])
    refreshed: list[list[str]] = []
    forced: list[bool] = []

    async def fake_refresh(codes):
        refreshed.append(sorted(codes))
        return len(codes)

    async def fake_get(*, force=False):
        forced.append(force)
        return set()

    async def run(svc, code, *, folder_id=None):
        return None  # keep rows pending

    async def not_flattened(code):
        return False

    async def no_active():
        return set()

    monkeypatch.setattr(presence_index, "refresh_codes", fake_refresh)
    monkeypatch.setattr(presence_index, "get", fake_get)
    monkeypatch.setattr(fin, "run_finalize", run)
    monkeypatch.setattr(arch, "_already_flattened", not_flattened)
    monkeypatch.setattr(arch, "_active_task_ids", no_active)

    await arch._finalize_retry_pass()
    assert refreshed == [["CCC-003", "CCC-004"]]
    assert forced == []  # no full-index read at all
    await engine.dispose()


# ---------------------------------------------------------------------------
# flatten-always: keepers land loose in the 系列 folder, code folder goes
# ---------------------------------------------------------------------------

def _series_graph():
    """series → CODE folder → (2 discs + junk)."""
    return {
        "series": [_folder("MIDV-001", "codef"),
                   _file("OTHER-99.mp4", "other", 2000)],
        "codef": [
            _file("midv-001-1.mp4", "d1", 3000),
            _file("midv-001-2.mp4", "d2", 2800),
            _file("ads.txt", "t", 0),
        ],
    }


def _patch_paths(monkeypatch, mapping):
    async def fake_resolve(code):
        return mapping["resolve"]
    import app.services.archiver as _arch
    monkeypatch.setattr(_arch, "_resolve_archive_path_by_code", fake_resolve)


async def test_flatten_moves_parts_to_series_and_removes_folder(monkeypatch):
    svc = FakeSvc(_series_graph(), path_ids={
        "AVBT/製作商/S/系/MIDV-001": "codef",
        "AVBT/製作商/S/系": "series",
    })
    _patch_paths(monkeypatch, {"resolve": "AVBT/製作商/S/系/MIDV-001"})
    events = [e async for e in finalize_code_folder_stream(
        svc, "MIDV-001", dry_run=False)]
    done = events[-1]
    assert done["type"] == "done" and done["result"]["errors"] == 0
    # parts renamed _1/_2, moved to the series folder, junk purged,
    # per-code folder to trash (recoverable — late discs may land in it)
    names = sorted(n.name for n in svc._graph["series"])
    assert names == ["MIDV-001_1.mp4", "MIDV-001_2.mp4", "OTHER-99.mp4"]
    assert "t" in svc.purged and "codef" in svc.trashed


async def test_flatten_defers_folder_delete_until_settled(monkeypatch):
    """Fresh moves close the settle gate: junk may go (it was never
    moved) but the code folder must survive this run, and run_finalize
    must NOT report success so the retry pass comes back later."""
    svc = FakeSvc(_series_graph(), path_ids={
        "AVBT/製作商/S/系/MIDV-001": "codef",
        "AVBT/製作商/S/系": "series",
    })
    svc.settled = False
    _patch_paths(monkeypatch, {"resolve": "AVBT/製作商/S/系/MIDV-001"})
    events = [e async for e in finalize_code_folder_stream(
        svc, "MIDV-001", dry_run=False)]
    done = events[-1]["result"]
    assert done["settling"] >= 1 and done["errors"] == 0
    assert "codef" not in svc.purged and "codef" not in svc.trashed
    # ^ folder survives the gate
    # keepers were still evacuated
    names = {n.name for n in svc._graph["series"]}
    assert {"MIDV-001_1.mp4", "MIDV-001_2.mp4"} <= names


async def test_run_finalize_returns_none_while_settling(monkeypatch):
    svc = FakeSvc(_series_graph(), path_ids={
        "AVBT/製作商/S/系/MIDV-001": "codef",
        "AVBT/製作商/S/系": "series",
    })
    svc.settled = False
    _patch_paths(monkeypatch, {"resolve": "AVBT/製作商/S/系/MIDV-001"})
    assert await run_finalize(svc, "MIDV-001") is None


async def test_shell_folder_purged_after_gate_opens(monkeypatch):
    """Second pass on an evacuated shell (videos already loose at the
    parent, junk left inside): once settled, junk purges and the code
    folder goes."""
    graph = {
        "series": [_folder("MIDV-001", "codef"),
                   _file("MIDV-001_1.mp4", "v1", 3000),
                   _file("MIDV-001_2.mp4", "v2", 2800)],
        "codef": [_file("ads.txt", "t", 0)],
    }
    svc = FakeSvc(graph, path_ids={
        "AVBT/製作商/S/系/MIDV-001": "codef",
        "AVBT/製作商/S/系": "series",
    })
    _patch_paths(monkeypatch, {"resolve": "AVBT/製作商/S/系/MIDV-001"})
    events = [e async for e in finalize_code_folder_stream(
        svc, "MIDV-001", dry_run=False)]
    done = events[-1]["result"]
    assert done["errors"] == 0 and not done.get("no_video")
    assert "t" in svc.purged and "codef" in svc.trashed
    assert sorted(n.name for n in svc._graph["series"]) == [
        "MIDV-001_1.mp4", "MIDV-001_2.mp4"]


async def test_flatten_uniquifies_against_series_siblings(monkeypatch):
    """An old low-res CODE.mp4 already loose in the series folder must
    not be overwritten — the new keeper gets a dedup suffix instead."""
    graph = {
        "series": [_folder("MIDV-001@bt", "codef"),
                   _file("MIDV-001.mp4", "old", 800)],
        "codef": [_file("[88K.ME]MIDV-001.mp4", "new", 4000)],
    }
    svc = FakeSvc(graph, path_ids={
        "AVBT/製作商/S/系/MIDV-001@bt": "codef",
        "AVBT/製作商/S/系": "series",
    })
    _patch_paths(monkeypatch, {"resolve": "AVBT/製作商/S/系/MIDV-001@bt"})
    events = [e async for e in finalize_code_folder_stream(
        svc, "MIDV-001", folder_id=None, dry_run=False)]
    assert events[-1]["result"]["errors"] == 0
    names = sorted(n.name for n in svc._graph["series"])
    assert "MIDV-001.mp4" in names          # old untouched
    assert "MIDV-001 (2).mp4" in names       # new deduped, not clobbered


async def test_explicit_folder_id_without_path_keeps_folder(monkeypatch):
    """Legacy behaviour: when the parent can't be resolved the folder
    stays (never guess a flatten destination)."""
    svc = FakeSvc(_wrapper_graph())
    events = await _collect(svc, "MIDV-001", "root", dry_run=False)
    assert events[-1]["result"]["errors"] == 0
    # keeper ends up in the code folder root, folder NOT removed
    assert [n.name for n in svc._graph["root"]] == ["MIDV-001.mp4"]
    assert "root" not in svc.purged and "root" not in svc.trashed


# ---------------------------------------------------------------------------
# orphan row reap (task vanished before file_id was tracked)
# ---------------------------------------------------------------------------

async def test_reap_closes_vanished_task_with_flattened_files(tmp_path, monkeypatch):
    now = datetime.utcnow()
    engine, maker = await _retry_db(tmp_path, monkeypatch, [
        OfflineTaskLog(code="MTM-013", magnet="m", task_id="t-gone", file_id="",
                       archived=False, finalized=False,
                       created_at=now - timedelta(hours=1)),
    ])

    async def no_active():
        return set()

    async def flattened(code):
        return True

    monkeypatch.setattr(arch, "_active_task_ids", no_active)
    monkeypatch.setattr(arch, "_already_flattened", flattened)
    assert await arch._reap_orphan_rows() == 1
    async with maker() as s:
        row = (await s.execute(select(OfflineTaskLog))).scalars().one()
    assert row.archived is True
    assert row.finalized is True
    assert row.archived_at is not None and row.finalized_at is not None
    assert "task gone" in (row.message or "")
    await engine.dispose()


async def test_reap_skips_task_still_in_list(tmp_path, monkeypatch):
    now = datetime.utcnow()
    engine, maker = await _retry_db(tmp_path, monkeypatch, [
        OfflineTaskLog(code="MTM-013", magnet="m", task_id="t-live", file_id="",
                       archived=False, finalized=False,
                       created_at=now - timedelta(hours=1)),
    ])

    async def active():
        return {"t-live"}

    async def flattened(code):  # pragma: no cover — must not be reached
        raise AssertionError("flattened check must not run for live tasks")

    monkeypatch.setattr(arch, "_active_task_ids", active)
    monkeypatch.setattr(arch, "_already_flattened", flattened)
    assert await arch._reap_orphan_rows() == 0
    async with maker() as s:
        row = (await s.execute(select(OfflineTaskLog))).scalars().one()
    assert row.archived is False and row.finalized is False
    await engine.dispose()


async def test_reap_skips_unflattened_code(tmp_path, monkeypatch):
    now = datetime.utcnow()
    engine, maker = await _retry_db(tmp_path, monkeypatch, [
        OfflineTaskLog(code="MTM-013", magnet="m", task_id="t-gone", file_id="",
                       archived=False, finalized=False,
                       created_at=now - timedelta(hours=1)),
    ])

    async def no_active():
        return set()

    async def not_flattened(code):
        return False

    monkeypatch.setattr(arch, "_active_task_ids", no_active)
    monkeypatch.setattr(arch, "_already_flattened", not_flattened)
    assert await arch._reap_orphan_rows() == 0
    async with maker() as s:
        row = (await s.execute(select(OfflineTaskLog))).scalars().one()
    assert row.archived is False and row.finalized is False
    await engine.dispose()


async def test_reap_ignores_fresh_and_file_id_rows(tmp_path, monkeypatch):
    now = datetime.utcnow()
    engine, maker = await _retry_db(tmp_path, monkeypatch, [
        # Inside the settle grace — a just-submitted Collecting task also
        # has file_id == "" and must not be touched.
        OfflineTaskLog(code="NEW-001", magnet="m", task_id="t-new", file_id="",
                       archived=False, finalized=False, created_at=now),
        # file_id known — the sweep's own stamp path owns this row.
        OfflineTaskLog(code="OLD-002", magnet="m", task_id="t-old",
                       file_id="f-123", archived=False, finalized=False,
                       created_at=now - timedelta(hours=2)),
    ])

    async def no_active():
        return set()

    async def flattened(code):
        return True

    monkeypatch.setattr(arch, "_active_task_ids", no_active)
    monkeypatch.setattr(arch, "_already_flattened", flattened)
    assert await arch._reap_orphan_rows() == 0
    async with maker() as s:
        rows = (await s.execute(select(OfflineTaskLog))).scalars().all()
    assert all(r.archived is False and r.finalized is False for r in rows)
    await engine.dispose()


async def test_reap_closes_archived_row_past_retry_window(tmp_path, monkeypatch):
    """An archived row whose finalize never landed must not strand.

    _finalize_retry_pass only looks back _FINALIZE_RETRY_WINDOW, and the
    reaper demanded archived=False with no file_id — so a row that was
    archived but never finalized fell through both once its archived_at
    aged out, staying finalized=0 forever even though its files were
    flattened and correct (live: 300MIUM-1276/1277/1295/1299/1319 and
    DVMM-380, archived 07-12 by the pre-#160 starved drain, still open at
    80h with clean single-file landings).
    """
    now = datetime.utcnow()
    engine, maker = await _retry_db(tmp_path, monkeypatch, [
        OfflineTaskLog(code="300MIUM-1295", magnet="m", task_id="t-gone",
                       file_id="f-set", archived=True, finalized=False,
                       created_at=now - timedelta(hours=80),
                       archived_at=now - timedelta(hours=80)),
    ])

    async def no_active():
        return set()

    async def flattened(code):
        return True

    monkeypatch.setattr(arch, "_active_task_ids", no_active)
    monkeypatch.setattr(arch, "_already_flattened", flattened)
    assert await arch._reap_orphan_rows() == 1
    async with maker() as s:
        row = (await s.execute(select(OfflineTaskLog))).scalars().one()
    assert row.finalized is True and row.finalized_at is not None
    await engine.dispose()


async def test_reap_leaves_archived_row_inside_retry_window(tmp_path, monkeypatch):
    """Inside the window the retry pass owns the row — it can still run a
    real finalize (which purges junk); the reaper must not close it out
    from under that and skip the cleanup."""
    now = datetime.utcnow()
    engine, maker = await _retry_db(tmp_path, monkeypatch, [
        OfflineTaskLog(code="FRESH-001", magnet="m", task_id="t-gone",
                       file_id="f-set", archived=True, finalized=False,
                       created_at=now - timedelta(hours=2),
                       archived_at=now - timedelta(hours=2)),
    ])

    async def no_active():
        return set()

    async def flattened(code):  # pragma: no cover — must not be reached
        raise AssertionError("retry pass still owns this row")

    monkeypatch.setattr(arch, "_active_task_ids", no_active)
    monkeypatch.setattr(arch, "_already_flattened", flattened)
    assert await arch._reap_orphan_rows() == 0
    async with maker() as s:
        row = (await s.execute(select(OfflineTaskLog))).scalars().one()
    assert row.finalized is False
    await engine.dispose()


async def test_reap_not_starved_by_fresh_active_rows(tmp_path, monkeypatch):
    """A burst of newer still-listed Collecting rows must not crowd an
    older genuine orphan out of the pass — active skips are free and
    only expensive flattened checks are capped."""
    now = datetime.utcnow()
    rows = [
        OfflineTaskLog(code=f"NEW-{i:03d}", magnet="m", task_id=f"t-live-{i}",
                       file_id="", archived=False, finalized=False,
                       created_at=now - timedelta(hours=1))
        for i in range(10)
    ]
    rows.append(
        OfflineTaskLog(code="MTM-013", magnet="m", task_id="t-gone", file_id="",
                       archived=False, finalized=False,
                       created_at=now - timedelta(hours=30)),
    )
    engine, maker = await _retry_db(tmp_path, monkeypatch, rows)

    async def active():
        return {f"t-live-{i}" for i in range(10)}

    async def flattened(code):
        return True

    monkeypatch.setattr(arch, "_active_task_ids", active)
    monkeypatch.setattr(arch, "_already_flattened", flattened)
    assert await arch._reap_orphan_rows() == 1
    async with maker() as s:
        by_code = {r.code: r for r in (await s.execute(select(OfflineTaskLog))).scalars()}
    assert by_code["MTM-013"].finalized is True
    assert all(not by_code[f"NEW-{i:03d}"].finalized for i in range(10))
    await engine.dispose()


async def test_reap_ignores_rows_outside_window(tmp_path, monkeypatch):
    """Historical rows (finalized column backfilled as 0) must never be
    scanned — only current-pipeline rows are candidates."""
    now = datetime.utcnow()
    engine, maker = await _retry_db(tmp_path, monkeypatch, [
        OfflineTaskLog(code="OLD-777", magnet="m", task_id="t-ancient",
                       file_id="", archived=False, finalized=False,
                       created_at=now - timedelta(days=30)),
    ])

    async def no_active():
        return set()

    async def flattened(code):  # pragma: no cover — must not be reached
        raise AssertionError("flattened check must not run outside the window")

    monkeypatch.setattr(arch, "_active_task_ids", no_active)
    monkeypatch.setattr(arch, "_already_flattened", flattened)
    assert await arch._reap_orphan_rows() == 0
    async with maker() as s:
        row = (await s.execute(select(OfflineTaskLog))).scalars().one()
    assert row.archived is False and row.finalized is False
    await engine.dispose()


async def test_reap_failed_checks_cool_down_and_free_the_cap(tmp_path, monkeypatch):
    """Zombie rows that keep failing the flattened check must not hog
    the per-pass cap: on the next pass they are in cooldown and the
    slots go to rows not yet attempted."""
    now = datetime.utcnow()
    rows = [
        OfflineTaskLog(code=f"ZOMBIE-{i:03d}", magnet="m", task_id=f"t-z-{i}",
                       file_id="", archived=False, finalized=False,
                       created_at=now - timedelta(days=3, hours=i))
        for i in range(arch._REAP_CHECK_LIMIT)
    ]
    rows.append(
        OfflineTaskLog(code="MTM-013", magnet="m", task_id="t-gone", file_id="",
                       archived=False, finalized=False,
                       created_at=now - timedelta(hours=30)),
    )
    engine, maker = await _retry_db(tmp_path, monkeypatch, rows)

    async def no_active():
        return set()

    async def flattened(code):
        return code == "MTM-013"

    monkeypatch.setattr(arch, "_active_task_ids", no_active)
    monkeypatch.setattr(arch, "_already_flattened", flattened)
    # Pass 1: the cap is spent on the older zombies.
    assert await arch._reap_orphan_rows() == 0
    # Pass 2: zombies are cooling down — the genuine orphan gets a slot.
    assert await arch._reap_orphan_rows() == 1
    async with maker() as s:
        by_code = {r.code: r for r in (await s.execute(select(OfflineTaskLog))).scalars()}
    assert by_code["MTM-013"].finalized is True
    assert all(not r.finalized for c, r in by_code.items() if c != "MTM-013")
    await engine.dispose()


async def test_finalize_retry_picks_up_collecting_orphan(tmp_path, monkeypatch):
    """A row submitted while Collecting never gets its file_id stamp, so
    the sweep moves the wrapper but archived stays 0 — and the retry
    pass used to ignore it forever (live: MUDR-349/358 wrappers full of
    junk 2h+ after landing). Task gone + finalize succeeds → the row is
    closed on both flags."""
    now = datetime.utcnow()
    engine, maker = await _retry_db(tmp_path, monkeypatch, [
        OfflineTaskLog(code="MUDR-349", magnet="m", task_id="t-gone",
                       file_id="", archived=False, finalized=False,
                       created_at=now - timedelta(hours=2)),
        # Still downloading — must be skipped via the active-task check.
        OfflineTaskLog(code="MUDR-999", magnet="m", task_id="t-live",
                       file_id="", archived=False, finalized=False,
                       created_at=now - timedelta(hours=2)),
    ])

    calls = []

    async def fake_run_finalize(svc, code, *, folder_id=None):
        calls.append(code)
        return {"errors": 0}

    async def active():
        return {"t-live"}

    monkeypatch.setattr(fin, "run_finalize", fake_run_finalize)
    monkeypatch.setattr(arch, "_active_task_ids", active)
    done = await arch._finalize_retry_pass()
    assert done == 1
    assert calls == ["MUDR-349"]

    async with maker() as s:
        rows = {r.code: r for r in (await s.execute(select(OfflineTaskLog))).scalars()}
    assert rows["MUDR-349"].finalized is True
    assert rows["MUDR-349"].archived is True
    assert rows["MUDR-349"].archived_at is not None
    assert rows["MUDR-999"].archived is False
    assert rows["MUDR-999"].finalized is False
    await engine.dispose()


async def test_finalize_retry_refreshes_only_attempted_codes(tmp_path, monkeypatch):
    """The presence refresh is ~1-2 live PikPak listings per code, so it
    must cover only the rows this pass will actually finalize. Rows held
    back by the active-task check or the failure cooldown are re-listed
    for nothing — and dead rows (task gone, nothing landed) stay selected
    for the whole 7-day orphan window, so refreshing every selected row
    re-listed 281 codes every 60s pass to find ~1 change (live 2026-07-15:
    sustained PikPak timeouts + a minutes-long archiver loop). The
    cooldown exists precisely to stop that re-listing."""
    now = datetime.utcnow()
    engine, _maker = await _retry_db(tmp_path, monkeypatch, [
        # Held back: its task is still downloading.
        OfflineTaskLog(code="MIDV-001", magnet="m", task_id="t-run",
                       archived=True, archived_at=now, finalized=False,
                       created_at=now - timedelta(hours=1)),
        # Held back: failed moments ago, still inside the cooldown.
        OfflineTaskLog(code="MIDV-002", magnet="m", archived=True,
                       archived_at=now, finalized=False,
                       created_at=now - timedelta(hours=1)),
        # The only row this pass will attempt.
        OfflineTaskLog(code="MIDV-003", magnet="m", archived=True,
                       archived_at=now, finalized=False,
                       created_at=now - timedelta(hours=1)),
    ])

    refreshed: list[str] = []

    async def spy_refresh(codes):
        refreshed.extend(codes)
        return 0

    from app.services.pikpak_presence import presence_index

    monkeypatch.setattr(presence_index, "refresh_codes", spy_refresh)

    async def fake_run_finalize(svc, code, *, folder_id=None):
        return {"errors": 0}

    async def active():
        return {"t-run"}

    monkeypatch.setattr(fin, "run_finalize", fake_run_finalize)
    monkeypatch.setattr(arch, "_active_task_ids", active)
    # MIDV-002 is row id 2 — park it in the cooldown.
    arch._finalize_attempts[2] = now

    done = await arch._finalize_retry_pass()
    assert done == 1
    assert refreshed == ["MIDV-003"]
    await engine.dispose()
