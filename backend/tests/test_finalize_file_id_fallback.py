"""Finalize a sweep-archived wrapper by its OfflineTaskLog file_id when
the BT name defeats the parser.

Live 2026-08-31 (round4 KAWD batch): phase-1 routes wrappers by
OfflineTaskLog code (#232), so ``kawd772hhbhd`` / ``1122kawd687FHD`` /
``0730-kawd734FHD`` landed in the right series folder — where finalize,
which finds folders by NAME (canonical path, then presence leaf parse),
could never see them: "找不到 KAWD-772 的歸檔資料夾" every pass, 6.6GB
video + ad clips sitting in the series folder for hours, reaper "kept
(archived, awaiting finalize)" forever. The row's file_id IS the wrapper.

Second shape (AP-370, same day): the name parses, but by-name resolution
RAISED ("File or folder is not found" from the presence path's canonical
walk) every pass for 3h. The fallback does not use that path.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import OfflineTaskLog
from app.services import archiver as arch
from app.services import finalize as fin
from app.services.finalize import finalize_code_folder_stream

MB = 1024 * 1024


def _folder(name, id):
    return SimpleNamespace(name=name, id=id, kind="drive#folder", size=None)


def _file(name, id, size_mb):
    return SimpleNamespace(name=name, id=id, kind="drive#file", size=size_mb * MB)


class FakeSvc:
    def __init__(self, graph, path_ids=None):
        self._graph = {k: list(v) for k, v in graph.items()}
        self._path_ids = dict(path_ids or {})
        self.moved = []
        self.renamed = []
        self.trashed = []
        self.purged = []
        self.lookups = []
        self.settled = True

    async def list_all_files(self, parent_id, *, cap=5000):
        return list(self._graph.get(parent_id, [])), False

    async def lookup_folder_id(self, path):
        self.lookups.append(path)
        return self._path_ids.get(path, "")

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

    def record_move_source(self, source_id):
        pass

    def move_settled(self, source_id):
        return self.settled


def _kawd_graph():
    """series folder P1 → wrapper W1 (unparseable BT name) with the film
    and the usual ad clutter, plus an unrelated loose sibling."""
    return {
        "P1": [_folder("kawd772hhbhd", "W1"), _file("KAWD-848.mp4", "sib", 6400)],
        "W1": [
            _file("kawd772hhb.mp4", "v1", 6300),
            _file("美女荷官自拍被干848.mp4", "ad1", 90),
            _file("ss.jpg", "ad2", 1),
            _folder("宣傳文件", "ads"),
        ],
        "ads": [_file("mo688.net.jpg", "ad3", 1)],
    }


async def _collect(svc, code, **kw):
    return [e async for e in finalize_code_folder_stream(svc, code, **kw)]


# ---------------------------------------------------------------------------
# finalize_code_folder_stream: caller-verified parent_id
# ---------------------------------------------------------------------------

async def test_explicit_parent_flattens_into_it_without_any_path_lookup():
    """The whole point: no by-name lookup can find ``kawd772hhbhd``, so
    the caller hands over the series folder it verified. The keeper is
    evacuated there under its canonical name, junk purged, wrapper shell
    trashed (settled) — exactly what a parseable wrapper gets."""
    svc = FakeSvc(_kawd_graph())
    events = await _collect(svc, "KAWD-772", folder_id="W1", parent_id="P1",
                            dry_run=False)
    assert events[-1]["type"] == "done"
    assert events[-1]["result"]["errors"] == 0
    assert (["v1"], "P1") in svc.moved
    assert ("v1", "KAWD-772.mp4") in svc.renamed
    names = {n.name for n in svc._graph["P1"]}
    assert "KAWD-772.mp4" in names and "KAWD-848.mp4" in names
    assert "W1" in svc.trashed, "emptied wrapper shell goes to trash"
    assert "sib" not in svc.trashed and "sib" not in svc.purged
    assert svc.lookups == [], "id-based: no path resolution at all"


async def test_explicit_parent_still_honours_the_settle_gate():
    svc = FakeSvc(_kawd_graph())
    svc.settled = False
    events = await _collect(svc, "KAWD-772", folder_id="W1", parent_id="P1",
                            dry_run=False)
    assert (["v1"], "P1") in svc.moved
    assert "W1" not in svc.trashed
    assert events[-1]["result"]["settling"] >= 1


async def test_parent_equal_to_folder_is_void_not_a_self_flatten():
    """A hint that names the wrapper itself must not plan a move into
    itself; it degrades to the legacy keep-the-folder path."""
    svc = FakeSvc(_kawd_graph())
    await _collect(svc, "KAWD-772", folder_id="W1", parent_id="W1",
                   dry_run=False)
    assert svc.moved == []
    assert ("v1", "KAWD-772.mp4") in svc.renamed


async def test_parent_hint_is_ignored_without_an_explicit_folder(monkeypatch):
    """parent_id only rides with folder_id — by-code resolution keeps
    its own parent derivation (and here finds nothing, as live)."""
    svc = FakeSvc(_kawd_graph())

    async def no_path(code):
        return "AVBT/製作商/kawaii/発掘！看板娘/KAWD-772"

    monkeypatch.setattr(arch, "_resolve_archive_path_by_code", no_path)

    async def no_hits(_svc, _code):
        return []

    monkeypatch.setattr(fin, "presence_code_folders", no_hits)
    events = await _collect(svc, "KAWD-772", parent_id="P1", dry_run=False)
    assert events[-1]["type"] == "error"
    assert "找不到" in events[-1]["message"]
    assert svc.moved == []


# ---------------------------------------------------------------------------
# eligibility + locate
# ---------------------------------------------------------------------------

def _row(**kw):
    base = dict(code="KAWD-772", magnet="m", name="kawd772hhbhd",
                file_id="W1", archived=True, finalized=False)
    base.update(kw)
    return OfflineTaskLog(**base)


def test_eligible_only_when_archived_with_file_id_and_unparseable_name():
    assert arch._file_id_fallback_eligible(_row()) is True
    assert arch._file_id_fallback_eligible(_row(name="1122kawd687FHD")) is True
    assert arch._file_id_fallback_eligible(_row(name="0730-kawd734FHD")) is True
    # name parses → by-name finalize can see the folder; not our case
    assert arch._file_id_fallback_eligible(_row(name="[FHD]KAWD-772")) is False
    assert arch._file_id_fallback_eligible(_row(archived=False)) is False
    assert arch._file_id_fallback_eligible(_row(file_id="")) is False


async def test_locate_returns_series_folder_only_for_a_direct_child_folder(monkeypatch):
    svc = FakeSvc(_kawd_graph(),
                  path_ids={"AVBT/製作商/kawaii/発掘！看板娘": "P1"})
    monkeypatch.setattr(arch, "pikpak_service", svc)

    async def path(code):
        return "AVBT/製作商/kawaii/発掘！看板娘/KAWD-772"

    monkeypatch.setattr(arch, "_resolve_archive_path_by_code", path)
    assert await arch._locate_archived_wrapper(_row()) == "P1"
    # a bare file id is not a wrapper
    assert await arch._locate_archived_wrapper(_row(file_id="sib")) == ""
    # not in the canonical series folder (routed to a twin) → leave alone
    assert await arch._locate_archived_wrapper(_row(file_id="elsewhere")) == ""


async def test_locate_swallows_lookup_failures(monkeypatch):
    class Boom(FakeSvc):
        async def lookup_folder_id(self, path):
            raise RuntimeError("PikPak down")

    monkeypatch.setattr(arch, "pikpak_service", Boom(_kawd_graph()))

    async def path(code):
        return "AVBT/製作商/kawaii/発掘！看板娘/KAWD-772"

    monkeypatch.setattr(arch, "_resolve_archive_path_by_code", path)
    assert await arch._locate_archived_wrapper(_row()) == ""


# ---------------------------------------------------------------------------
# retry pass wiring
# ---------------------------------------------------------------------------

async def _retry_db(tmp_path, monkeypatch, rows):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(arch, "SessionLocal", maker)

    from app.services.pikpak_presence import presence_index

    async def fake_refresh(codes):
        return 0

    monkeypatch.setattr(presence_index, "refresh_codes", fake_refresh)

    async def no_active():
        return set()

    monkeypatch.setattr(arch, "_active_task_ids", no_active)
    arch._finalize_attempts.clear()
    arch._reap_attempts.clear()
    async with maker() as s:
        s.add_all(rows)
        await s.commit()
    return engine, maker


def _aged_row(**kw):
    now = datetime.utcnow()
    return _row(archived_at=now - timedelta(hours=1),
                created_at=now - timedelta(hours=1), **kw)


async def test_pass_falls_back_to_file_id_when_by_name_finds_nothing(tmp_path, monkeypatch):
    engine, maker = await _retry_db(tmp_path, monkeypatch, [_aged_row()])
    calls = []

    async def fake_run_finalize(svc, code, *, folder_id=None, parent_id=None, **_kw):
        calls.append((code, folder_id, parent_id))
        if folder_id is None:
            return None  # "找不到 KAWD-772 的歸檔資料夾"
        assert (folder_id, parent_id) == ("W1", "P1")
        return {"errors": 0}

    async def locate(row):
        return "P1"

    monkeypatch.setattr(fin, "run_finalize", fake_run_finalize)
    monkeypatch.setattr(arch, "_locate_archived_wrapper", locate)

    assert await arch._finalize_retry_pass() == 1
    assert calls == [("KAWD-772", None, None), ("KAWD-772", "W1", "P1")]
    async with maker() as s:
        row = (await s.execute(select(OfflineTaskLog))).scalars().one()
    assert row.finalized is True
    await engine.dispose()


async def test_pass_falls_back_when_by_name_raises(tmp_path, monkeypatch):
    """AP-370 shape: by-name resolution raised every pass; previously
    that was the end of the row for another backoff window."""
    engine, maker = await _retry_db(tmp_path, monkeypatch, [_aged_row()])

    async def fake_run_finalize(svc, code, *, folder_id=None, parent_id=None, **_kw):
        if folder_id is None:
            raise RuntimeError("File or folder is not found")
        return {"errors": 0}

    async def locate(row):
        return "P1"

    monkeypatch.setattr(fin, "run_finalize", fake_run_finalize)
    monkeypatch.setattr(arch, "_locate_archived_wrapper", locate)

    assert await arch._finalize_retry_pass() == 1
    async with maker() as s:
        row = (await s.execute(select(OfflineTaskLog))).scalars().one()
    assert row.finalized is True
    await engine.dispose()


async def test_pass_does_not_fall_back_for_a_parseable_name(tmp_path, monkeypatch):
    """By-name finalize found the folder and declined (settling) — the
    id path must not re-run the same plan on the same folder."""
    engine, maker = await _retry_db(
        tmp_path, monkeypatch, [_aged_row(name="[FHD]KAWD-772")])
    located = []

    async def fake_run_finalize(svc, code, *, folder_id=None, parent_id=None, **_kw):
        return None

    async def locate(row):
        located.append(row.code)
        return "P1"

    async def not_flat(code, **_kw):
        return False

    monkeypatch.setattr(fin, "run_finalize", fake_run_finalize)
    monkeypatch.setattr(arch, "_locate_archived_wrapper", locate)
    monkeypatch.setattr(arch, "_already_flattened", not_flat)

    assert await arch._finalize_retry_pass() == 0
    assert located == []
    async with maker() as s:
        row = (await s.execute(select(OfflineTaskLog))).scalars().one()
    assert row.finalized is False
    await engine.dispose()


async def test_pass_leaves_row_in_backoff_when_wrapper_not_located(tmp_path, monkeypatch):
    engine, maker = await _retry_db(tmp_path, monkeypatch, [_aged_row()])

    async def fake_run_finalize(svc, code, *, folder_id=None, parent_id=None, **_kw):
        assert folder_id is None, "no id-based run without a located parent"
        return None

    async def locate(row):
        return ""

    async def not_flat(code, **_kw):
        return False

    monkeypatch.setattr(fin, "run_finalize", fake_run_finalize)
    monkeypatch.setattr(arch, "_locate_archived_wrapper", locate)
    monkeypatch.setattr(arch, "_already_flattened", not_flat)

    assert await arch._finalize_retry_pass() == 0
    assert 1 in arch._finalize_attempts or len(arch._finalize_attempts) == 1
    await engine.dispose()
