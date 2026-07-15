"""Reorganize re-home pass: existing <kind>/<name>/<code> folders migrate
into the nested 製作商/<studio>/<series>/<code> layout (dry-run)."""

import json
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.archiver as arch
import app.services.reorganize as reorg
from app.database import Base
from app.models import MovieDetailCache


def _node(name, id, folder=True):
    return SimpleNamespace(
        name=name, id=id, kind="drive#folder" if folder else "drive#file"
    )


class FakePikpak:
    """Path-addressable fake. ``children`` maps folder-id -> [nodes];
    ``path_ids`` maps a folder path -> id. Unknown paths get a synthetic
    empty folder so dry-run parent resolution never explodes."""

    def __init__(self, path_ids, children):
        self._path_ids = dict(path_ids)
        self._children = dict(children)
        self._folder_cache = {}
        self._next = 0

    async def folder_id(self, path):
        if path not in self._path_ids:
            self._next += 1
            self._path_ids[path] = f"auto{self._next}"
        return self._path_ids[path]

    async def lookup_folder_id(self, path):
        return self._path_ids.get(path)

    async def list_files(self, parent_id, size=500):
        return list(self._children.get(parent_id, []))

    async def move_files(self, ids, parent_id):  # not hit in dry-run
        raise AssertionError("dry-run must not move")

    async def rename_file(self, fid, name):
        raise AssertionError("dry-run must not rename")


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


async def test_rehome_moves_flat_series_code_to_nested(tmp_path, monkeypatch):
    # DB with the detail so _resolve_archive_path_by_code returns nested.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/r.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(arch, "SessionLocal", maker)     # detail-cache read
    monkeypatch.setattr(reorg, "SessionLocal", maker)    # tracked-listing read
    async with maker() as s:
        s.add(_cache_row("MIDV-001", ("プレステージ", "75"), ("回胴録", "11pb")))
        await s.commit()

    # Fake PikPak: AVBT/系列/回胴録/MIDV-001 exists (flat legacy layout).
    path_ids = {
        "AVBT": "root",
        "AVBT/已完成": "legacy",
        "AVBT/系列": "kSeries",
        "AVBT/系列/回胴録": "nameDir",
    }
    children = {
        "root": [],              # kind dirs skipped at root anyway
        "legacy": [],
        "kSeries": [_node("回胴録", "nameDir")],
        "nameDir": [_node("MIDV-001", "mvfolder")],
    }
    fake = FakePikpak(path_ids, children)
    monkeypatch.setattr(reorg, "pikpak_service", fake)
    monkeypatch.setattr(arch, "pikpak_service", fake)

    events = [
        ev async for ev in reorg.reorganize_stream(dry_run=True, rehome_kinds=True)
    ]

    moves = [e for e in events if e.get("action") == "move"]
    assert any(
        e.get("target") == "AVBT/製作商/プレステージ/回胴録/MIDV-001"
        for e in moves
    ), f"no nested move; moves={[e.get('target') for e in moves]}"
    done = [e for e in events if e.get("type") == "done"]
    assert done and done[0]["result"]["dry_run"] is True
    await engine.dispose()


async def test_rehome_off_by_default_no_kind_move(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/r2.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(arch, "SessionLocal", maker)
    monkeypatch.setattr(reorg, "SessionLocal", maker)
    async with maker() as s:
        s.add(_cache_row("MIDV-001", ("プレステージ", "75"), ("回胴録", "11pb")))
        await s.commit()

    path_ids = {
        "AVBT": "root", "AVBT/已完成": "legacy",
        "AVBT/系列": "kSeries", "AVBT/系列/回胴録": "nameDir",
    }
    children = {
        "root": [], "legacy": [],
        "kSeries": [_node("回胴録", "nameDir")],
        "nameDir": [_node("MIDV-001", "mvfolder")],
    }
    fake = FakePikpak(path_ids, children)
    monkeypatch.setattr(reorg, "pikpak_service", fake)
    monkeypatch.setattr(arch, "pikpak_service", fake)

    events = [
        ev async for ev in reorg.reorganize_stream(dry_run=True, rehome_kinds=False)
    ]
    # Without rehome, phase-1c never runs → no migrate move for MIDV-001.
    assert not any(
        e.get("action") == "move" and "製作商" in (e.get("target") or "")
        for e in events
    )
    await engine.dispose()


def test_phase1_file_leaf_scene_markers_keep_original():
    # Old-scene disc markers need whole-group context to number — the
    # migrate pass must NOT collapse them onto the bare code (phase-2
    # renumbers after landing).
    assert reorg._phase1_file_leaf(
        "-SOE00829HHB3.wmv", "SOE-829", ".wmv") == "-SOE00829HHB3.wmv"
    assert reorg._phase1_file_leaf(
        "OFJE-296CD1-B.mp4", "OFJE-296", ".mp4") == "OFJE-296CD1-B.mp4"
    # ``_N`` markers are still normalised inline …
    assert reorg._phase1_file_leaf(
        "hhd800.com@SOE-462_1.mp4", "SOE-462", ".mp4") == "SOE-462_1.mp4"
    # … and plain names still get the default code leaf.
    assert reorg._phase1_file_leaf(
        "kfa55.com@DAM-043.mp4", "DAM-043", ".mp4") == "DAM-043.mp4"


class LiveFakePikpak(FakePikpak):
    """Mutation-recording fake for a dry_run=False rehome pass."""

    def __init__(self, path_ids, children):
        super().__init__(path_ids, children)
        self.moved = []
        self.renamed = []
        self.move_sources = []
        self.trashed = []

    async def move_files(self, ids, parent_id):
        self.moved.append((list(ids), parent_id))
        return {}

    async def rename_file(self, fid, name):
        self.renamed.append((fid, name))
        return {}

    def record_move_source(self, source_id):
        self.move_sources.append(source_id)

    def move_settled(self, source_id):  # gate never opens in this test
        return False

    async def _trash_if_empty(self, folder_id, protect_ids=frozenset()):
        if not self.move_settled(folder_id):
            return False
        self.trashed.append(folder_id)
        return True


async def test_rehome_live_records_move_source_and_defers_shell_trash(
    tmp_path, monkeypatch
):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/r3.db", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(arch, "SessionLocal", maker)
    monkeypatch.setattr(reorg, "SessionLocal", maker)
    async with maker() as s:
        s.add(_cache_row("MIDV-001", ("プレステージ", "75"), ("回胴録", "11pb")))
        await s.commit()

    path_ids = {
        "AVBT": "root",
        "AVBT/已完成": "legacy",
        "AVBT/系列": "kSeries",
        "AVBT/系列/回胴録": "nameDir",
    }
    children = {
        "root": [], "legacy": [],
        "kSeries": [_node("回胴録", "nameDir")],
        "nameDir": [_node("MIDV-001", "mvfolder")],
    }
    fake = LiveFakePikpak(path_ids, children)
    monkeypatch.setattr(reorg, "pikpak_service", fake)
    monkeypatch.setattr(arch, "pikpak_service", fake)

    events = [
        ev async for ev in reorg.reorganize_stream(
            dry_run=False, rehome_kinds=True
        )
    ]

    assert any(e.get("action") == "move" for e in events)
    assert (["mvfolder"], fake._path_ids[
        "AVBT/製作商/プレステージ/回胴録"]) in [
        (ids, pid) for ids, pid in fake.moved
    ]
    # The settle gate must be armed for the folder the move left …
    assert "nameDir" in fake.move_sources
    # … and the emptied shell must NOT be trashed while unsettled.
    assert fake.trashed == []
    await engine.dispose()
