"""Recursive "整理此資料夾": descend grouping folders to 番號 leaves, move
misplaced ones to 製作商/<studio>/<系列>/, normalise in place, and trash
folders left empty. Drives ``PikPakService.cleanup_folder_stream``."""

import json
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.archiver as arch
import app.services.pikpak as pk
from app.database import Base
from app.models import MovieDetailCache

MB = 1024 * 1024


def _folder(name, id):
    return SimpleNamespace(name=name, id=id, kind="drive#folder", size=None)


def _file(name, id, size_mb=600):
    return SimpleNamespace(
        name=name, id=id, kind="drive#file", size=size_mb * MB
    )


class FakeSvc(pk.PikPakService):
    """Real cleanup logic over an in-memory, mutable node graph. Records
    every mutator call so tests can assert exactly what happened."""

    def __init__(self, path_ids, children):
        super().__init__()
        self._path_ids = dict(path_ids)
        self._graph = {k: list(v) for k, v in children.items()}
        self.moved = []    # (ids, dest_parent_id)
        self.renamed = []  # (id, new_name)
        self.trashed = []  # id

    async def list_all_files(self, parent_id, *, cap=5000):
        return list(self._graph.get(parent_id, [])), False

    async def list_files(self, parent_id, size=100):
        return list(self._graph.get(parent_id, []))

    async def folder_id(self, path):
        return self._path_ids.get(path, f"auto:{path}")

    async def lookup_folder_id(self, path):
        return self._path_ids.get(path)

    def _parent_of(self, node_id):
        for pid, kids in self._graph.items():
            for n in kids:
                if n.id == node_id:
                    return pid, n
        return None, None

    async def move_files(self, ids, parent_id):
        self.moved.append((list(ids), parent_id))
        for nid in ids:
            pid, node = self._parent_of(nid)
            if pid is not None:
                self._graph[pid] = [n for n in self._graph[pid] if n.id != nid]
                self._graph.setdefault(parent_id, []).append(node)
        return {}

    async def rename_file(self, fid, new_name):
        self.renamed.append((fid, new_name))
        _pid, node = self._parent_of(fid)
        if node is not None:
            node.name = new_name
        return {}

    async def trash_files(self, ids):
        self.trashed.append(list(ids)[0] if len(ids) == 1 else list(ids))
        for nid in ids:
            pid, _n = self._parent_of(nid)
            if pid is not None:
                self._graph[pid] = [n for n in self._graph[pid] if n.id != nid]
        return {}


def _cache_row(code, studio, series):
    detail = {
        "code": code, "title": "t",
        "studio": {"name": studio[0], "id": studio[1]},
        "series": {"name": series[0], "id": series[1]},
        "actresses": [], "genres": [], "samples": [], "magnets": [],
    }
    return MovieDetailCache(
        code=code, detail=json.dumps(detail), release_date="",
        fetched_at=datetime.utcnow(),
    )


async def _db(tmp_path, monkeypatch, rows):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/c.db", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(arch, "SessionLocal", maker)  # _resolve reads cache
    async with maker() as s:
        s.add_all(rows)
        await s.commit()
    return engine


def _run(svc, folder_id, *, dry_run):
    async def collect():
        return [e async for e in svc.cleanup_folder_stream(folder_id, dry_run=dry_run)]
    return collect()


# --- graph shared by several cases: 製作商 → studio → series → 番號 ---
def _nested_graph(wrapper_parent="series"):
    """MIDV-001 wrapper (one 600MB video) lives under ``wrapper_parent``
    (``series`` = correct 回胴録 folder; ``wrong`` = a sibling series)."""
    path_ids = {
        "AVBT": "root",
        "AVBT/已完成": "legacy",
        "AVBT/製作商": "kStudio",
        "AVBT/製作商/プレステージ/回胴録": "series",
    }
    parent = "series" if wrapper_parent == "series" else "wrong"
    graph = {
        "kStudio": [_folder("プレステージ", "studio")],
        "studio": [_folder("回胴録", "series"), _folder("別系列", "wrong")],
        "series": [] if parent != "series" else [_folder("MIDV-001", "wrap")],
        "wrong": [] if parent != "wrong" else [_folder("MIDV-001", "wrap")],
        "wrap": [_file("MIDV-001.mp4", "vid")],
    }
    return path_ids, graph


async def test_descend_two_levels_to_code_leaf(tmp_path, monkeypatch):
    engine = await _db(
        tmp_path, monkeypatch,
        [_cache_row("MIDV-001", ("プレステージ", "75"), ("回胴録", "11pb"))],
    )
    path_ids, graph = _nested_graph("series")
    svc = FakeSvc(path_ids, graph)
    events = await _run(svc, "kStudio", dry_run=False)

    # Recursion reached the leaf two grouping levels down and flattened it
    # into its (already-correct) series folder.
    flat = [e for e in events if e.get("action") == "flatten"]
    assert flat, f"no flatten; actions={[e.get('action') for e in events]}"
    assert (["vid"], "series") in svc.moved      # video pulled into 回胴録
    assert "wrap" in svc.trashed                 # emptied wrapper trashed
    await engine.dispose()


async def test_move_misplaced_loose_file(tmp_path, monkeypatch):
    engine = await _db(
        tmp_path, monkeypatch,
        [_cache_row("MIDV-001", ("プレステージ", "75"), ("回胴録", "11pb"))],
    )
    # A loose file sitting in the WRONG series folder.
    path_ids = {
        "AVBT": "root", "AVBT/已完成": "legacy", "AVBT/製作商": "kStudio",
        "AVBT/製作商/プレステージ/回胴録": "series",
    }
    graph = {
        "kStudio": [_folder("プレステージ", "studio")],
        "studio": [_folder("回胴録", "series"), _folder("別系列", "wrong")],
        "series": [],
        "wrong": [_file("MIDV-001.mp4", "vid")],
    }
    svc = FakeSvc(path_ids, graph)
    events = await _run(svc, "kStudio", dry_run=False)

    moves = [e for e in events if e.get("action") == "move"]
    assert any(
        e.get("target") == "AVBT/製作商/プレステージ/回胴録/MIDV-001.mp4"
        for e in moves
    ), f"targets={[e.get('target') for e in moves]}"
    assert (["vid"], "series") in svc.moved      # moved into the 回胴録 id
    # 'wrong' folder now empty → trashed.
    assert "wrong" in svc.trashed
    await engine.dispose()


async def test_skip_codeless_loose_file_keeps_folder(tmp_path, monkeypatch):
    engine = await _db(tmp_path, monkeypatch, [])
    path_ids = {
        "AVBT": "root", "AVBT/已完成": "legacy", "AVBT/製作商": "kStudio",
    }
    graph = {
        "kStudio": [_folder("プレステージ", "studio")],
        "studio": [_file("random-notes.mp4", "loose")],
    }
    svc = FakeSvc(path_ids, graph)
    events = await _run(svc, "kStudio", dry_run=False)

    assert any(e.get("reason") == "no_code" for e in events)
    assert svc.moved == [] and svc.trashed == []   # nothing moved, folder kept
    await engine.dispose()


async def test_already_at_target_no_redundant_move(tmp_path, monkeypatch):
    engine = await _db(
        tmp_path, monkeypatch,
        [_cache_row("MIDV-001", ("プレステージ", "75"), ("回胴録", "11pb"))],
    )
    # The loose file already sits in its correct 回胴録 folder.
    path_ids = {
        "AVBT": "root", "AVBT/已完成": "legacy", "AVBT/製作商": "kStudio",
        "AVBT/製作商/プレステージ/回胴録": "series",
    }
    graph = {
        "kStudio": [_folder("プレステージ", "studio")],
        "studio": [_folder("回胴録", "series")],
        "series": [_file("MIDV-001.mp4", "vid")],
    }
    svc = FakeSvc(path_ids, graph)
    events = await _run(svc, "kStudio", dry_run=False)

    # Same parent id → never a move (guards the "don't move to current
    # folder" rejection); name already canonical → skip.
    assert svc.moved == []
    assert not any(e.get("action") == "move" for e in events)
    await engine.dispose()


async def test_dry_run_zero_mutations(tmp_path, monkeypatch):
    engine = await _db(
        tmp_path, monkeypatch,
        [_cache_row("MIDV-001", ("プレステージ", "75"), ("回胴録", "11pb"))],
    )
    path_ids, graph = _nested_graph("wrong")   # misplaced wrapper
    svc = FakeSvc(path_ids, graph)
    events = await _run(svc, "kStudio", dry_run=True)

    assert svc.moved == [] and svc.renamed == [] and svc.trashed == []
    done = [e for e in events if e.get("type") == "done"]
    assert done and done[0]["result"]["dry_run"] is True
    # Preview still reports the would-be flatten/trash.
    assert any(e.get("action") in ("flatten", "move") for e in events)
    await engine.dispose()


async def test_empty_grouping_folder_trashed_but_not_kind_base(tmp_path, monkeypatch):
    engine = await _db(
        tmp_path, monkeypatch,
        [_cache_row("MIDV-001", ("プレステージ", "75"), ("回胴録", "11pb"))],
    )
    # Wrapper in the WRONG series → its video leaves → both the wrapper and
    # the emptied wrong-series folder get trashed; the studio empties too.
    path_ids, graph = _nested_graph("wrong")
    svc = FakeSvc(path_ids, graph)
    await _run(svc, "kStudio", dry_run=False)

    assert "wrap" in svc.trashed and "wrong" in svc.trashed
    # Never the selected root or the kind base.
    assert "kStudio" not in svc.trashed
    await engine.dispose()


async def test_root_and_kind_base_protected(tmp_path, monkeypatch):
    engine = await _db(tmp_path, monkeypatch, [])
    # Root itself would empty out (its only child is a codeless folder that
    # itself empties), but the root is protected and never trashed.
    path_ids = {
        "AVBT": "root", "AVBT/已完成": "legacy", "AVBT/製作商": "kStudio",
    }
    graph = {"kStudio": [_folder("プレステージ", "studio")], "studio": []}
    svc = FakeSvc(path_ids, graph)
    await _run(svc, "kStudio", dry_run=False)

    assert "kStudio" not in svc.trashed        # selected root + kind base
    # An already-empty studio has removed==0 → not "became empty by us" →
    # left alone as well.
    assert "studio" not in svc.trashed
    await engine.dispose()


async def test_trash_if_empty_contract():
    # The shared helper reorganize's re-home cleanup also relies on:
    # trash an empty, unprotected folder; never a non-empty one (re-list
    # gate); never a protected one (e.g. the kind base) even when empty.
    svc = FakeSvc({}, {"empty": [], "full": [_file("x.mp4", "x")]})
    assert await svc._trash_if_empty("empty", protect_ids=frozenset()) is True
    assert "empty" in svc.trashed
    assert await svc._trash_if_empty("full", protect_ids=frozenset()) is False
    svc2 = FakeSvc({}, {"base": []})
    assert (
        await svc2._trash_if_empty("base", protect_ids=frozenset({"base"}))
        is False
    )
    assert svc2.trashed == []


async def test_depth_cap_stops_descent(tmp_path, monkeypatch):
    engine = await _db(tmp_path, monkeypatch, [])
    # 6 nested grouping folders, code leaf only at the very bottom. With a
    # depth cap of 4 the descent must not reach (or mutate) the leaf.
    path_ids = {"AVBT": "root", "AVBT/已完成": "legacy", "AVBT/製作商": "kStudio"}
    graph = {"kStudio": [_folder("g0", "g0")]}
    for i in range(6):
        graph[f"g{i}"] = [_folder(f"g{i + 1}", f"g{i + 1}")]
    graph["g6"] = [_file("MIDV-001.mp4", "deepvid")]
    svc = FakeSvc(path_ids, graph)
    await _run(svc, "kStudio", dry_run=False)

    assert (["deepvid"], svc._path_ids.get("x")) not in svc.moved
    assert all("deepvid" not in ids for ids, _p in svc.moved)
    await engine.dispose()


async def test_collect_main_videos_counts_transferring_file_as_main():
    """A file PikPak is still writing has an unknown final size — it must
    count as a main video so the single-video flatten (which trashes the
    wrapper around it) is blocked until the task completes."""
    partial = SimpleNamespace(name="IDBD-924-2.mp4", id="d2",
                              kind="drive#file", size=120 * MB,
                              phase="PHASE_TYPE_RUNNING")
    done = _file("IDBD-924-1.mp4", "d1", 10000)
    svc = FakeSvc({}, {"wrap": [done, partial]})
    top, total = await svc._collect_main_videos("wrap", 300 * MB)
    assert total == 2  # blocks flatten
    assert {v.id for v in top} == {"d1", "d2"}
