"""Presence index persistence: the archived-code answer lives in the DB,
so a restart costs a query — not a full PikPak drive walk."""


from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.pikpak_presence as pres
from app.database import Base
from app.models import PresenceEntry
from app.services.pikpak_presence import PikPakPresenceIndex


async def _db(tmp_path, monkeypatch, name="p.db"):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/{name}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(pres, "SessionLocal", maker)
    return engine, maker

def _idx(build_result=None, on_build=None):
    idx = PikPakPresenceIndex()

    async def fake_build():
        if on_build:
            on_build()
        idx._paths = {c: [f"AVBT/製作商/S/X/{c}"] for c in (build_result or set())}
        return set(build_result or set())

    idx._build = fake_build  # type: ignore[method-assign]
    return idx

async def test_rebuild_persists_and_restart_loads_without_walking(
    tmp_path, monkeypatch
):
    engine, maker = await _db(tmp_path, monkeypatch)
    builds = []
    idx = _idx({"MIDV-001", "ABC-002"}, on_build=lambda: builds.append(1))
    assert await idx.rebuild(force=True) == {"MIDV-001", "ABC-002"}
    assert len(builds) == 1
    async with maker() as s:
        rows = (await s.execute(select(PresenceEntry))).scalars().all()
    assert {r.code for r in rows} == {"MIDV-001", "ABC-002"}

    # "Restart": a fresh instance must answer from the DB, no walk.
    fresh_builds = []
    fresh = _idx({"SHOULD-NOT-BUILD"}, on_build=lambda: fresh_builds.append(1))
    assert await fresh.get() == {"MIDV-001", "ABC-002"}
    assert fresh_builds == []
    assert fresh.built_at is not None
    await engine.dispose()

async def test_get_bootstraps_when_db_empty(tmp_path, monkeypatch):
    engine, _ = await _db(tmp_path, monkeypatch, "p2.db")
    builds = []
    idx = _idx({"NEW-001"}, on_build=lambda: builds.append(1))
    assert await idx.get() == {"NEW-001"}
    assert len(builds) == 1  # nothing persisted yet → one bootstrap walk
    await engine.dispose()

async def test_invalidate_does_not_arm_a_walk(tmp_path, monkeypatch):
    engine, _ = await _db(tmp_path, monkeypatch, "p3.db")
    builds = []
    idx = _idx({"KEEP-001"}, on_build=lambda: builds.append(1))
    await idx.get()
    idx.invalidate()
    assert idx.status()["stale"] is True
    assert await idx.get() == {"KEEP-001"}
    assert len(builds) == 1  # still just the bootstrap — no re-walk
    await engine.dispose()

async def test_refresh_codes_updates_and_drops(tmp_path, monkeypatch):
    engine, maker = await _db(tmp_path, monkeypatch, "p4.db")
    idx = _idx({"MOVED-001", "GONE-002"})
    await idx.get()

    live = {
        "MOVED-001": ["AVBT/製作商/S/Y/MOVED-001.mp4"],  # renamed + moved
        "GONE-002": [],                                   # deleted
    }

    async def fake_live(code):
        return live[code]

    idx._live_paths_for = fake_live  # type: ignore[method-assign]
    changed = await idx.refresh_codes(["MOVED-001", "GONE-002"])
    assert changed == 2
    assert idx.paths_for("MOVED-001") == ["AVBT/製作商/S/Y/MOVED-001.mp4"]
    assert idx.paths_for("GONE-002") == []
    assert "GONE-002" not in await idx.get()

    async with maker() as s:
        rows = (await s.execute(select(PresenceEntry))).scalars().all()
    assert {(r.code, r.path) for r in rows} == {
        ("MOVED-001", "AVBT/製作商/S/Y/MOVED-001.mp4")
    }
    await engine.dispose()

async def test_live_paths_never_fetch_details(tmp_path, monkeypatch):
    """A cache miss on the per-code hot path must not hit JavBus."""
    engine, _ = await _db(tmp_path, monkeypatch, "p5.db")
    import app.services.archiver as arch

    async def boom(*a, **k):
        raise AssertionError("presence refresh must not fetch JavBus")

    monkeypatch.setattr(arch.scraper, "fetch_detail_resolved", boom)
    idx = PikPakPresenceIndex()
    idx._paths = {"OLD-001": ["AVBT/製作商/S/X/OLD-001.mp4"]}

    listed: list[str] = []

    async def fake_lookup(path):
        listed.append(path)
        return ""

    monkeypatch.setattr(pres.pikpak_service, "lookup_folder_id", fake_lookup)
    assert await idx._live_paths_for("OLD-001") == []
    # It re-listed where the code was last seen (parent of the known path).
    assert "AVBT/製作商/S/X" in listed
    await engine.dispose()

def test_status_reports_stale_flag():
    idx = PikPakPresenceIndex()
    assert idx.status()["stale"] is False
    idx.invalidate()
    assert idx.status()["stale"] is True
