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
    # junk permanently gone (files then folders), nothing merely trashed
    assert set(svc.purged) == {"txt", "ad", "s1", "smp", "wrap"}
    assert svc.trashed == []
    # root now holds exactly the canonical video
    assert [n.name for n in svc._graph["root"]] == ["MIDV-001.mp4"]


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
    assert "wrap" not in svc.purged


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
    assert not any(f in svc.purged for f in ("bn", "ex", "wrap"))
    # The deep video is untouched.
    assert any(n.id == "deep" for n in svc._graph["bn"])


async def test_folder_purge_failure_protects_its_ancestors():
    class FlakyPurge(FakeSvc):
        async def delete_forever(self, ids):
            if "smp" in ids:
                raise RuntimeError("boom")
            return await super().delete_forever(ids)

    svc = FlakyPurge(_wrapper_graph())
    events = await _collect(svc, "MIDV-001", "root", dry_run=False)
    errs = [e for e in events if e.get("action") == "error" and e.get("kind") == "folder"]
    assert errs and errs[0]["source"] == "Sample"
    # Sample failed to purge → wrap must NOT be purged around it.
    assert "wrap" not in svc.purged

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
