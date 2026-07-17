"""Ad-shell wrappers (files, zero video/container) must be trashed, not
archived — archiving one mints a canonical-looking 番號 folder that every
layer reads as success and nothing ever re-sends the code (live: EDD-138,
then OYC-205)."""

from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.archiver as arch
from app.database import Base
from app.models import OfflineTaskLog
from app.services.finalize import wrapper_is_ad_shell

MB = 1024 * 1024


def _folder(name, id):
    return SimpleNamespace(name=name, id=id, kind="drive#folder", size=None)


def _file(name, id, size_mb=600, phase=""):
    return SimpleNamespace(
        name=name, id=id, kind="drive#file", size=size_mb * MB, phase=phase
    )


class Svc:
    def __init__(self, graph, *, partial=False):
        self._graph = graph
        self._partial = partial
        self.trashed = []

    async def list_all_files(self, parent_id, *, cap=5000):
        return list(self._graph.get(parent_id, [])), self._partial

    async def trash_files(self, ids):
        self.trashed.append(list(ids))
        return {}


ADS = [
    _file("1024社區.jpg", "a1", 1),
    _file("最新地址.txt", "a2", 0),
    _file("宣傳.url", "a3", 0),
]


async def test_pure_ads_is_shell():
    assert await wrapper_is_ad_shell(Svc({"w": ADS}), "w") is True


async def test_any_video_is_content():
    svc = Svc({"w": ADS + [_file("EDD-138.mp4", "v", 1500)]})
    assert await wrapper_is_ad_shell(svc, "w") is False


async def test_container_is_content_not_junk():
    # A lone CODE.iso belongs to the container-swap loop, not the trash.
    svc = Svc({"w": ADS + [_file("SNIS-494.iso", "c", 23000)]})
    assert await wrapper_is_ad_shell(svc, "w") is False


async def test_nested_video_is_content():
    svc = Svc({"w": ADS + [_folder("CD1", "sub")],
               "sub": [_file("EDD-138_1.avi", "v", 900)]})
    assert await wrapper_is_ad_shell(svc, "w") is False


async def test_nested_ads_only_is_shell():
    svc = Svc({"w": [_folder("廣告", "sub")], "sub": ADS})
    assert await wrapper_is_ad_shell(svc, "w") is True


async def test_empty_listing_proves_nothing():
    # Optimistic listings show freshly-moved folders as empty (#140) —
    # and a file id lists as empty too. Neither may be condemned.
    assert await wrapper_is_ad_shell(Svc({"w": []}), "w") is False


async def test_truncated_listing_proves_nothing():
    svc = Svc({"w": ADS}, partial=True)
    assert await wrapper_is_ad_shell(svc, "w") is False


async def test_transferring_file_defers_judgement():
    # Mid-download only the ads may be visible/complete; judge later.
    svc = Svc({"w": ADS + [_file("part", "p", 1, phase="PHASE_TYPE_RUNNING")]})
    assert await wrapper_is_ad_shell(svc, "w") is False


# ---------------------------------------------------------------------------
# sweep row bookkeeping
# ---------------------------------------------------------------------------

async def test_mark_shell_trashed_orphans_row(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/s.db", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(arch, "SessionLocal", maker)
    async with maker() as s:
        s.add_all([
            OfflineTaskLog(code="OYC-205", magnet="m", file_id="shell-1",
                           archived=False),
            OfflineTaskLog(code="MIDV-001", magnet="m", file_id="keep-1",
                           archived=False),
        ])
        await s.commit()

    await arch._mark_offline_log_shell_trashed(["shell-1"])

    async with maker() as s:
        rows = {r.code: r for r in
                (await s.execute(select(OfflineTaskLog))).scalars()}
    # file_id cleared: the DB-driven pass stops matching the trashed id,
    # archived stays False so the dead-code scan re-sends the code.
    assert rows["OYC-205"].file_id == ""
    assert rows["OYC-205"].archived is False
    assert "ad-shell" in rows["OYC-205"].message
    assert rows["MIDV-001"].file_id == "keep-1"
    await engine.dispose()


# ---------------------------------------------------------------------------
# phase-1 gate
# ---------------------------------------------------------------------------

class Phase1Svc(Svc):
    def __init__(self, graph, children, **kw):
        super().__init__(graph, **kw)
        self._children = children
        self.moved = []

    async def folder_id(self, path):
        return "src"

    async def list_files(self, parent_id, size=500):
        return list(self._children)

    async def move_files(self, ids, parent_id):
        self.moved.append((list(ids), parent_id))
        return {}


async def test_phase1_trashes_shell_instead_of_migrating(monkeypatch):
    from app.services import reorganize

    shell = _folder("[1024]OYC-205", "shell-1")
    svc = Phase1Svc({"shell-1": ADS}, [shell])
    monkeypatch.setattr(reorganize, "pikpak_service", svc)

    events = [ev async for ev in reorganize._phase1_migrate_from(
        "AVBT/TASK", dry_run=False, idx_start=0
    )]
    actions = [(ev.get("action"), ev.get("reason"))
               for ev in events if ev.get("type") == "progress"]
    assert ("trash", "ad_shell") in actions
    assert svc.trashed == [["shell-1"]]
    assert svc.moved == []


async def test_phase1_dry_run_reports_but_keeps_shell(monkeypatch):
    from app.services import reorganize

    shell = _folder("[1024]OYC-205", "shell-1")
    svc = Phase1Svc({"shell-1": ADS}, [shell])
    monkeypatch.setattr(reorganize, "pikpak_service", svc)

    events = [ev async for ev in reorganize._phase1_migrate_from(
        "AVBT/TASK", dry_run=True, idx_start=0
    )]
    assert any(ev.get("action") == "trash" for ev in events)
    assert svc.trashed == []
