"""Persistent detail cache: write-through on fetch, restart-surviving
reads, recency-aware TTL, refresh bypass, and self-healing on bad rows."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.scrapers.javbus as jb
import app.services.detail_cache as dc
from app.database import Base
from app.models import MovieDetailCache
from app.schemas import MovieDetail


async def _fresh_db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/cache.db", echo=False, future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(dc, "SessionLocal", sessionmaker)
    return engine, sessionmaker


def _patch_scraper(monkeypatch, *, title="Some Title", release_date="2020-01-01"):
    """Route fetch_detail's HTTP + parse through counters/fakes."""
    calls = {"n": 0}

    async def fake_fetch(cli, url, **kwargs):
        calls["n"] += 1
        return "<html>stub</html>"

    def fake_parse(html, code):
        # Carry one genre: rows without genres are now treated as
        # parse-stale (pre-genre-fix cache) and deliberately refetch.
        from app.schemas import GenreRef

        return MovieDetail(code=code, title=title, release_date=release_date,
                           genres=[GenreRef(name="g", id="1")])

    monkeypatch.setattr(jb, "_fetch", fake_fetch)
    monkeypatch.setattr(jb, "_parse_detail", fake_parse)
    monkeypatch.setattr(jb, "_get_client", lambda: object())
    jb._detail_cache.clear()
    return calls


async def _row(sessionmaker, code):
    async with sessionmaker() as session:
        return (
            await session.execute(
                select(MovieDetailCache).where(MovieDetailCache.code == code)
            )
        ).scalar_one_or_none()


async def test_fetch_writes_through_and_survives_restart(tmp_path, monkeypatch):
    engine, sm = await _fresh_db(tmp_path, monkeypatch)
    calls = _patch_scraper(monkeypatch)

    detail = await jb.fetch_detail("ABC-123")
    assert detail.title == "Some Title"
    assert calls["n"] == 1
    row = await _row(sm, "ABC-123")
    assert row is not None and row.release_date == "2020-01-01"

    # Simulate a restart: in-memory cache gone, DB row remains.
    jb._detail_cache.clear()
    again = await jb.fetch_detail("ABC-123")
    assert again.title == "Some Title"
    assert calls["n"] == 1  # no second HTTP fetch
    # And the DB hit refilled the in-memory layer.
    assert jb._detail_cache_get("ABC-123") is not None
    await engine.dispose()


async def test_recent_release_expires_fast_old_stays_fresh(tmp_path, monkeypatch):
    engine, sm = await _fresh_db(tmp_path, monkeypatch)

    recent_date = (datetime.utcnow() - timedelta(days=5)).strftime("%Y-%m-%d")
    await dc.put("NEW-1", MovieDetail(code="NEW-1", title="t", release_date=recent_date))
    await dc.put("OLD-1", MovieDetail(code="OLD-1", title="t", release_date="2015-06-01"))

    # Backdate both rows by 2 days: past the 1-day recent TTL, well
    # within the 30-day old TTL.
    async with sm() as session:
        for code in ("NEW-1", "OLD-1"):
            row = await session.get(MovieDetailCache, code)
            row.fetched_at = datetime.utcnow() - timedelta(days=2)
        await session.commit()

    assert await dc.get("NEW-1") is None
    assert (await dc.get("OLD-1")) is not None

    # Missing release_date counts as recent.
    await dc.put("NODATE-1", MovieDetail(code="NODATE-1", title="t"))
    async with sm() as session:
        row = await session.get(MovieDetailCache, "NODATE-1")
        row.fetched_at = datetime.utcnow() - timedelta(days=2)
        await session.commit()
    assert await dc.get("NODATE-1") is None
    await engine.dispose()


async def test_refresh_bypasses_read_and_rewrites_row(tmp_path, monkeypatch):
    engine, sm = await _fresh_db(tmp_path, monkeypatch)
    calls = _patch_scraper(monkeypatch, title="v2")

    await dc.put("ABC-123", MovieDetail(code="ABC-123", title="v1"))
    detail = await jb.fetch_detail("ABC-123", refresh=True)
    assert detail.title == "v2"
    assert calls["n"] == 1  # went to HTTP despite a fresh DB row
    row = await _row(sm, "ABC-123")
    assert "v2" in row.detail
    await engine.dispose()


async def test_empty_title_not_persisted(tmp_path, monkeypatch):
    engine, sm = await _fresh_db(tmp_path, monkeypatch)
    _patch_scraper(monkeypatch, title="")

    detail = await jb.fetch_detail("GONE-404")
    assert detail.title == ""
    assert await _row(sm, "GONE-404") is None
    await engine.dispose()


async def test_corrupt_row_is_a_miss(tmp_path, monkeypatch):
    engine, sm = await _fresh_db(tmp_path, monkeypatch)
    async with sm() as session:
        session.add(
            MovieDetailCache(code="BAD-1", detail="{not json", release_date="2015-01-01")
        )
        await session.commit()
    assert await dc.get("BAD-1") is None
    await engine.dispose()


async def test_disabled_flag_short_circuits(tmp_path, monkeypatch):
    engine, sm = await _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(dc.settings, "javbus_persist_cache_enabled", False)
    await dc.put("ABC-1", MovieDetail(code="ABC-1", title="t"))
    assert await _row(sm, "ABC-1") is None
    assert await dc.get("ABC-1") is None
    await engine.dispose()


async def test_resolved_alias_persisted_under_queried_code(tmp_path, monkeypatch):
    engine, sm = await _fresh_db(tmp_path, monkeypatch)
    jb._detail_cache.clear()

    real_detail = MovieDetail(code="259LUXU-1543", title="amateur", release_date="2021-01-01")

    async def fake_fetch_detail(code, *, refresh=False):
        if code == "259LUXU-1543":
            await dc.put(code, real_detail)
            return real_detail
        return MovieDetail(code=code, title="")

    async def fake_search(code):
        return "259LUXU-1543"

    monkeypatch.setattr(jb, "fetch_detail", fake_fetch_detail)
    monkeypatch.setattr(jb, "_search_canonical_code", fake_search)

    out = await jb.fetch_detail_resolved("LUXU-1543")
    assert out.title == "amateur"
    # Persisted under BOTH the canonical and the queried (stripped) code.
    assert (await _row(sm, "259LUXU-1543")) is not None
    assert (await _row(sm, "LUXU-1543")) is not None
    await engine.dispose()
