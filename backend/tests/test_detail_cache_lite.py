"""Batch cache-join read for browse-card metadata (get_many_lite +
POST /api/javbus/details/cached): hit/miss mix, TTL deliberately
ignored, malformed rows skipped, cap enforced, and — the whole point of
this endpoint — never touches the scraper."""

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.detail_cache as dc
from app.database import Base
from app.models import MovieDetailCache
from app.routers.javbus import _CACHED_DETAILS_CAP, cached_details
from app.schemas import GenreRef, LinkRef, MovieDetail


async def _fresh_db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/cache.db", echo=False, future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(dc, "SessionLocal", sessionmaker)
    return engine, sessionmaker


def _detail(code, *, studio=None, series=None, genres=None, release_date="2020-01-01"):
    return MovieDetail(
        code=code,
        title="t",
        release_date=release_date,
        studio=LinkRef(name=studio, id=studio.lower()) if studio else None,
        series=LinkRef(name=series, id=series.lower()) if series else None,
        genres=[GenreRef(name=g) for g in (genres or [])],
    )


async def test_hit_miss_mix(tmp_path, monkeypatch):
    engine, sm = await _fresh_db(tmp_path, monkeypatch)
    await dc.put("ABC-1", _detail("ABC-1", studio="S1", series="Ser1", genres=["G1", "G2"]))
    await dc.put("ABC-2", _detail("ABC-2"))  # no studio/series/genres

    out = await dc.get_many_lite(["ABC-1", "ABC-2", "NOPE-404"])

    assert set(out) == {"ABC-1", "ABC-2"}  # absent code omitted, not an error
    assert out["ABC-1"]["studio"] == {"name": "S1", "id": "s1"}
    assert out["ABC-1"]["series"] == {"name": "Ser1", "id": "ser1"}
    assert out["ABC-1"]["genres"] == ["G1", "G2"]
    assert out["ABC-2"]["studio"] is None
    assert out["ABC-2"]["series"] is None
    assert out["ABC-2"]["genres"] == []
    await engine.dispose()


async def test_genres_truncated_to_four(tmp_path, monkeypatch):
    engine, sm = await _fresh_db(tmp_path, monkeypatch)
    await dc.put("ABC-1", _detail("ABC-1", genres=["G1", "G2", "G3", "G4", "G5", "G6"]))
    out = await dc.get_many_lite(["ABC-1"])
    assert out["ABC-1"]["genres"] == ["G1", "G2", "G3", "G4"]
    await engine.dispose()


async def test_stale_row_still_returned(tmp_path, monkeypatch):
    """get() would call this a miss past the recent-release TTL; the
    lite projection deliberately ignores that — studio/series/genres
    are immutable, so a stale row is still an honest answer."""
    engine, sm = await _fresh_db(tmp_path, monkeypatch)
    recent_date = (datetime.utcnow() - timedelta(days=5)).strftime("%Y-%m-%d")
    await dc.put("NEW-1", _detail("NEW-1", studio="S1", release_date=recent_date))
    async with sm() as session:
        row = await session.get(MovieDetailCache, "NEW-1")
        row.fetched_at = datetime.utcnow() - timedelta(days=2)  # past the 1-day recent TTL
        await session.commit()

    assert await dc.get("NEW-1") is None  # TTL-aware read: a miss
    out = await dc.get_many_lite(["NEW-1"])  # lite read: still a hit
    assert out["NEW-1"]["studio"] == {"name": "S1", "id": "s1"}
    await engine.dispose()


async def test_malformed_json_row_skipped(tmp_path, monkeypatch):
    engine, sm = await _fresh_db(tmp_path, monkeypatch)
    await dc.put("GOOD-1", _detail("GOOD-1", studio="S1"))
    async with sm() as session:
        session.add(
            MovieDetailCache(code="BAD-1", detail="{not json", release_date="2020-01-01")
        )
        await session.commit()

    out = await dc.get_many_lite(["GOOD-1", "BAD-1"])
    assert set(out) == {"GOOD-1"}
    await engine.dispose()


async def test_empty_codes_short_circuits_without_query(tmp_path, monkeypatch):
    engine, sm = await _fresh_db(tmp_path, monkeypatch)
    assert await dc.get_many_lite([]) == {}
    await engine.dispose()


async def test_disabled_flag_returns_empty(tmp_path, monkeypatch):
    engine, sm = await _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(dc.settings, "javbus_persist_cache_enabled", False)
    await dc.put("ABC-1", _detail("ABC-1", studio="S1"))
    assert await dc.get_many_lite(["ABC-1"]) == {}
    await engine.dispose()


async def test_never_calls_the_scraper(tmp_path, monkeypatch):
    """The whole point of this endpoint: cache-join only, no network."""
    engine, sm = await _fresh_db(tmp_path, monkeypatch)
    await dc.put("ABC-1", _detail("ABC-1", studio="S1"))

    def boom(*a, **kw):
        raise AssertionError("get_many_lite must never call the scraper")

    import app.scrapers.javbus as jb

    monkeypatch.setattr(jb, "fetch_detail", boom)
    monkeypatch.setattr(jb, "fetch_detail_resolved", boom)
    monkeypatch.setattr(jb, "_fetch", boom)

    out = await dc.get_many_lite(["ABC-1", "MISSING-CODE"])
    assert out["ABC-1"]["studio"] == {"name": "S1", "id": "s1"}
    assert "MISSING-CODE" not in out  # a genuine miss, not a scrape-and-fill
    await engine.dispose()


# ---------- router: cap + shape ----------

async def test_router_cap_truncates_beyond_60(tmp_path, monkeypatch):
    engine, sm = await _fresh_db(tmp_path, monkeypatch)
    codes = [f"C-{i:03d}" for i in range(70)]
    assert len(codes) > _CACHED_DETAILS_CAP

    seen: list[list[str]] = []
    orig = dc.get_many_lite

    async def spy(cs):
        seen.append(list(cs))
        return await orig(cs)

    monkeypatch.setattr(dc, "get_many_lite", spy)
    # The router module imported `detail_cache` as a module reference, so
    # patching the module's own attribute (above) is what it calls through.
    resp = await cached_details(codes=codes)
    assert seen and len(seen[0]) == _CACHED_DETAILS_CAP
    assert resp.items == {}  # none of these codes are cached, but no error
    await engine.dispose()


async def test_router_returns_hits_only(tmp_path, monkeypatch):
    engine, sm = await _fresh_db(tmp_path, monkeypatch)
    await dc.put("ABC-1", _detail("ABC-1", studio="S1", series="Ser1", genres=["G1"]))

    resp = await cached_details(codes=["ABC-1", "ZZZ-404"])
    assert set(resp.items) == {"ABC-1"}
    item = resp.items["ABC-1"]
    assert item.code == "ABC-1"
    assert item.studio == LinkRef(name="S1", id="s1")
    assert item.series == LinkRef(name="Ser1", id="ser1")
    assert item.genres == ["G1"]
    await engine.dispose()
