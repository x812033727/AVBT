"""pCloud organize mirrors the PikPak 製作商/<studio>/<series> nesting."""

import json
from datetime import datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.archiver as arch
from app.database import Base
from app.models import MovieDetailCache
from app.schemas import LinkRef, MovieDetail
from app.services.pcloud_organize import PCloudOrganizeMixin


def _detail(code, studio=None, series=None):
    return MovieDetail(
        code=code, title="t",
        studio=LinkRef(name=studio[0], id=studio[1]) if studio else None,
        series=LinkRef(name=series[0], id=series[1]) if series else None,
    )


async def test_studio_series_dir_and_path(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/pn.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(
        arch, "SessionLocal", async_sessionmaker(engine, expire_on_commit=False)
    )
    arch._tracked_name_cache.clear()
    d = _detail("YRK-288", studio=("PrestigeStudio", "75"), series=("TowerSeries", "u0g"))
    assert await arch._studio_series_dir(d) == "AVBT/製作商/PrestigeStudio/TowerSeries"
    assert await arch._studio_series_path(d, "YRK-288") == \
        "AVBT/製作商/PrestigeStudio/TowerSeries/YRK-288"
    await engine.dispose()


async def test_studio_series_dir_no_studio_is_none():
    assert await arch._studio_series_dir(_detail("X-1", studio=None)) is None


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


async def test_nested_or_kind_target_prefers_studio(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/p.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(arch, "SessionLocal", maker)
    async with maker() as s:
        s.add(_cache_row("YRK-288", ("PrestigeStudio", "75"), ("TowerSeries", "u0g")))
        await s.commit()

    class Dummy(PCloudOrganizeMixin):
        pass

    obj = Dummy()
    # Has studio → nested, ignoring the loose (kind, name).
    t = await obj._nested_or_kind_target("YRK-288", "series", "SomeSeriesName")
    assert t == "AVBT/製作商/PrestigeStudio/TowerSeries"
    await engine.dispose()


async def test_nested_or_kind_target_falls_back_without_studio(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/p2.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(arch, "SessionLocal", maker)

    async def no_fetch(*a, **k):
        return None
    monkeypatch.setattr(arch.scraper, "fetch_detail_resolved", no_fetch)

    class Dummy(PCloudOrganizeMixin):
        pass

    obj = Dummy()
    # No cache row + fetch None → fall back to single-kind folder.
    t = await obj._nested_or_kind_target("NOS-001", "label", "SomeLabel")
    assert t == "AVBT/發行商/SomeLabel"
    await engine.dispose()
