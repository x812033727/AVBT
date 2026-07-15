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

    async def fake_live(code, *, exclude_ids=frozenset()):
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


class _Entry:
    """Minimal stand-in for a PikPakFile (only .id / .name are read)."""

    def __init__(self, id: str, name: str) -> None:
        self.id = id
        self.name = name


async def test_refresh_codes_drops_ids_finalize_just_removed(
    tmp_path, monkeypatch
):
    """A wrapper finalize just trashed must not survive in the index.

    PikPak's listing is eventually consistent: for a window after
    trash_files() the dead folder is still returned. Recomputing paths
    from that listing reproduces the cached set byte for byte, so the
    change check skips the write and the phantom path outlives the
    folder (only an unrelated full walk ever cleared it).
    """
    engine, maker = await _db(tmp_path, monkeypatch, "p_ghost.db")
    code = "GTAL-005"
    series = "AVBT/製作商/ゴールデンタイム/未分類"
    wrapper = _Entry("wrap-1", "[Initial D] GTAL-005")
    video = _Entry("vid-1", "GTAL-005.mp4")

    idx = PikPakPresenceIndex()
    idx._codes = {code}
    idx._paths = {code: [f"{series}/{wrapper.name}", f"{series}/{video.name}"]}
    await idx._persist_code(code, idx._paths[code])

    import app.services.archiver as arch

    async def _no_nested(_code, allow_fetch=False):
        return None

    monkeypatch.setattr(arch, "studio_series_dir_for_code", _no_nested)

    class _Svc:
        async def lookup_folder_id(self, path):
            return "series-id" if path == series else None

        async def list_all_files(self, parent_id="", cap=0):
            return [wrapper, video], False

    monkeypatch.setattr(pres, "pikpak_service", _Svc())

    changed = await idx.refresh_codes([code], exclude_ids={wrapper.id})

    assert changed == 1
    assert idx.paths_for(code) == [f"{series}/{video.name}"]
    async with maker() as s:
        rows = (await s.execute(select(PresenceEntry))).scalars().all()
    assert [r.path for r in rows] == [f"{series}/{video.name}"]
    await engine.dispose()


async def test_no_missing_path_forces_a_drive_walk():
    """Guard the whole module, not one call site: #169 fixed the two
    obvious ones and left the streaming-summary and missing-all paths
    still forcing a 2.5-min full walk. A grep-level invariant is the
    cheapest way to keep the contract from eroding again."""
    from pathlib import Path

    src = Path("app/services/missing.py").read_text(encoding="utf-8")
    assert "presence_index.get(force=refresh)" not in src, (
        "listing refresh must not force a presence drive walk — "
        "the index is persisted (#163) and refreshed per-code"
    )
