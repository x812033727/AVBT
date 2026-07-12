"""Archiver path resolution: 製作商/<studio>/<series>/<code> nesting."""

import json
from datetime import datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.archiver as arch
from app.database import Base
from app.models import MovieDetailCache
from app.schemas import LinkRef, MovieDetail


def _detail(code, studio=None, series=None):
    return MovieDetail(
        code=code,
        title="t",
        studio=LinkRef(name=studio[0], id=studio[1]) if studio else None,
        series=LinkRef(name=series[0], id=series[1]) if series else None,
    )


def test_studio_series_path_nested():
    d = _detail("YRK-288", studio=("PrestigeStudio", "75"), series=("TowerSeries", "u0g"))
    p = arch._studio_series_path(d, "YRK-288")
    assert p == "AVBT/製作商/PrestigeStudio/TowerSeries/YRK-288"


def test_studio_series_path_no_series_bucket():
    d = _detail("ABF-001", studio=("PrestigeStudio", "75"), series=None)
    p = arch._studio_series_path(d, "ABF-001")
    assert p == "AVBT/製作商/PrestigeStudio/未分類/ABF-001"


def test_studio_series_path_none_without_studio():
    d = _detail("X-1", studio=None, series=("s", "s1"))
    assert arch._studio_series_path(d, "X-1") is None


def _cache_row(code, studio=None, series=None):
    detail = {
        "code": code, "title": "t",
        "studio": {"name": studio[0], "id": studio[1]} if studio else None,
        "series": {"name": series[0], "id": series[1]} if series else None,
        "actresses": [], "genres": [], "samples": [], "magnets": [],
    }
    return MovieDetailCache(
        code=code, detail=json.dumps(detail), release_date="", fetched_at=datetime.utcnow()
    )


async def _seed(tmp_path, monkeypatch, rows):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/arch.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(arch, "SessionLocal", maker)
    async with maker() as session:
        session.add_all(rows)
        await session.commit()
    return engine


async def test_resolve_by_code_uses_cache_row(tmp_path, monkeypatch):
    engine = await _seed(
        tmp_path, monkeypatch,
        [_cache_row("YRK-288", studio=("PrestigeStudio", "75"), series=("TowerSeries", "u0g"))],
    )
    # Guard: must NOT hit the network when the cache row exists.
    async def boom(*a, **k):
        raise AssertionError("should not fetch when cached")
    monkeypatch.setattr(arch.scraper, "fetch_detail_resolved", boom)

    path = await arch._resolve_archive_path_by_code("YRK-288")
    await engine.dispose()
    assert path == "AVBT/製作商/PrestigeStudio/TowerSeries/YRK-288"


async def test_resolve_by_code_fetches_when_uncached(tmp_path, monkeypatch):
    engine = await _seed(tmp_path, monkeypatch, [])

    async def fake_fetch(code, *a, **k):
        return _detail(code, studio=("Moodyz", "4v"), series=None)
    monkeypatch.setattr(arch.scraper, "fetch_detail_resolved", fake_fetch)

    path = await arch._resolve_archive_path_by_code("MIDE-999")
    await engine.dispose()
    assert path == "AVBT/製作商/Moodyz/未分類/MIDE-999"


async def test_resolve_by_code_no_studio_falls_back_to_archive_folder(tmp_path, monkeypatch):
    engine = await _seed(tmp_path, monkeypatch, [_cache_row("NOS-001", studio=None, series=None)])
    # no-studio → resolve_listing_for_code; make it find nothing tracked.
    async def fake_fetch(code, *a, **k):
        return _detail(code, studio=None, series=None)
    monkeypatch.setattr(arch.scraper, "fetch_detail_resolved", fake_fetch)

    path = await arch._resolve_archive_path_by_code("NOS-001")
    await engine.dispose()
    from app.config import settings
    assert path == f"{settings.pikpak_archive_folder}/NOS-001"
