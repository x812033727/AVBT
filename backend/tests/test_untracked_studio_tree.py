"""2026-07-20 alignment rule: the main 製作商 tree holds ONLY studios
the user tracks; works of untracked studios archive under 其他製作商."""

from datetime import datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.archiver as arch
from app.config import studio_scan_bases, untracked_studio_base_path
from app.database import Base
from app.models import MovieDetailCache, TrackedListing
from app.schemas import MovieDetail


def _detail(code: str, studio_id: str, studio_name: str) -> MovieDetail:
    return MovieDetail.model_validate({
        "code": code, "title": "t",
        "studio": {"name": studio_name, "id": studio_id},
        "series": {"name": "某系列", "id": "s1"},
        "actresses": [], "genres": [], "samples": [], "magnets": [],
    })


async def _db(tmp_path, monkeypatch, rows):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(arch, "SessionLocal", maker)
    arch._tracked_name_cache.clear()
    async with maker() as s:
        s.add_all(rows)
        await s.commit()
    return engine


def _tracked_row(sid: str, name: str) -> TrackedListing:
    return TrackedListing(
        kind="studio", id=sid, name=name, avatar="", uncensored=False,
        auto_send=False, last_seen_code="", last_error="", new_count=0,
        created_at=datetime(2026, 7, 20),
    )


async def test_tracked_studio_stays_in_main_tree(tmp_path, monkeypatch):
    engine = await _db(tmp_path, monkeypatch, [_tracked_row("75", "プレステージ")])
    path = await arch._studio_series_dir(_detail("X-1", "75", "プレステージ"))
    assert path == "AVBT/製作商/プレステージ/某系列"
    await engine.dispose()


async def test_untracked_studio_goes_to_sibling_tree(tmp_path, monkeypatch):
    engine = await _db(tmp_path, monkeypatch, [])
    path = await arch._studio_series_dir(_detail("X-1", "3xl", "Magic"))
    assert path == "AVBT/其他製作商/Magic/某系列"
    await engine.dispose()


async def test_resolver_full_path_follows_tracking(tmp_path, monkeypatch):
    import json
    detail_json = json.dumps({
        "code": "ZZZ-001", "title": "t",
        "studio": {"name": "GIGA", "id": "eh"},
        "series": {"name": "ヒロイン陵辱", "id": "606"},
        "actresses": [], "genres": [], "samples": [], "magnets": [],
    })
    engine = await _db(tmp_path, monkeypatch, [
        MovieDetailCache(
            code="ZZZ-001", detail=detail_json, release_date="",
            fetched_at=datetime(2026, 7, 20),
        ),
    ])
    path = await arch._resolve_archive_path_by_code("ZZZ-001")
    assert path == "AVBT/其他製作商/GIGA/ヒロイン陵辱/ZZZ-001"
    await engine.dispose()


def test_studio_scan_bases_covers_both_roots():
    bases = studio_scan_bases()
    assert bases[0].endswith("製作商")
    assert bases[1] == untracked_studio_base_path()
    assert bases[1].endswith("其他製作商")
    assert bases[0] != bases[1]
