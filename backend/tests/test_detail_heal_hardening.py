"""Hardening for the genre-heal path (integration audit 2026-07-18).

The heal turned every pre-fix cache row (genres=[]) into a network miss
for ALL fetch_detail callers, and a failed heal LOST data the caller
already had: exceptions propagated (502 to views) and empty pages
returned an empty MovieDetail instead of the stale row. Failed heals
also never advanced ``fetched_at`` (put() skips empty titles), wedging
the backfill's oldest-first window on the same unfixable rows.

Pinned here:
- a failed heal (exception or empty page) serves the stale row;
- a failed heal touches ``fetched_at`` so the backfill window rotates;
- heal is attempted once per code per process (no refetch storms, and a
  genuinely tagless title stops refetching after its first heal);
- refresh=True keeps raising (manual refresh must surface errors);
- the backfill cycle breaks after consecutive failures (outage guard);
- put() logs studio/series identity drift when overwriting a row.
"""

import logging

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.scrapers.javbus as jb
import app.services.detail_cache as dc
from app.database import Base
from app.models import MovieDetailCache
from app.schemas import GenreRef, LinkRef, MovieDetail


async def _mk_db(tmp_path, monkeypatch, name):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/{name}.db", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(dc, "SessionLocal", sm)
    return engine, sm


def _wire_scraper(monkeypatch, fetch):
    monkeypatch.setattr(jb, "_fetch", fetch)
    monkeypatch.setattr(jb, "_get_client", lambda: object())
    monkeypatch.setattr(jb, "_heal_attempted", set())
    jb._detail_cache.clear()

    async def fake_magnets(*a, **kw):
        return []

    monkeypatch.setattr(jb, "_fetch_magnets", fake_magnets)


async def _fetched_at(sm, code):
    async with sm() as session:
        row = (
            await session.execute(
                select(MovieDetailCache).where(MovieDetailCache.code == code)
            )
        ).scalar_one()
        return row.fetched_at


async def _age_row(sm, code, dt):
    async with sm() as session:
        row = (
            await session.execute(
                select(MovieDetailCache).where(MovieDetailCache.code == code)
            )
        ).scalar_one()
        row.fetched_at = dt
        await session.commit()


async def test_failed_heal_serves_stale_row_and_touches(tmp_path, monkeypatch):
    """Heal fetch blows up → caller gets the stale row (never a 502) and
    fetched_at advances so the backfill window rotates past the code."""
    from datetime import datetime, timedelta

    engine, sm = await _mk_db(tmp_path, monkeypatch, "fail")
    await dc.put("HARD-1", MovieDetail(code="HARD-1", title="t", release_date="2020-01-01"))
    # Inside the old-release persist TTL (a month) — expired rows are a
    # plain refetch, not a heal, and keep their raise-on-failure path.
    old = datetime.utcnow() - timedelta(days=10)
    await _age_row(sm, "HARD-1", old)

    calls = {"n": 0}

    async def boom(cli, url, **kw):
        calls["n"] += 1
        raise RuntimeError("javbus down")

    _wire_scraper(monkeypatch, boom)

    d = await jb.fetch_detail("HARD-1")
    assert calls["n"] == 1
    assert d.title == "t"  # stale row, not an empty detail / exception
    assert await _fetched_at(sm, "HARD-1") > old  # window advanced
    await engine.dispose()


async def test_empty_html_heal_serves_stale_row(tmp_path, monkeypatch):
    """Removed listing (empty page) during a heal → stale row, not an
    empty MovieDetail."""
    engine, sm = await _mk_db(tmp_path, monkeypatch, "empty")
    await dc.put("HARD-2", MovieDetail(code="HARD-2", title="t"))

    async def empty(cli, url, **kw):
        return ""

    _wire_scraper(monkeypatch, empty)

    d = await jb.fetch_detail("HARD-2")
    assert d.title == "t"
    await engine.dispose()


async def test_heal_attempted_once_per_process(tmp_path, monkeypatch):
    """A failing heal must not retry on every call — one shot per code
    per process, then the stale row serves as a plain cache hit."""
    engine, sm = await _mk_db(tmp_path, monkeypatch, "once")
    await dc.put("HARD-3", MovieDetail(code="HARD-3", title="t"))

    calls = {"n": 0}

    async def boom(cli, url, **kw):
        calls["n"] += 1
        raise RuntimeError("javbus down")

    _wire_scraper(monkeypatch, boom)

    d1 = await jb.fetch_detail("HARD-3")
    d2 = await jb.fetch_detail("HARD-3")
    d3 = await jb.fetch_detail("HARD-3")
    assert calls["n"] == 1
    assert d1.title == d2.title == d3.title == "t"
    await engine.dispose()


async def test_tagless_title_stops_refetching_after_heal(tmp_path, monkeypatch):
    """A heal that succeeds but still finds no genres (genuinely tagless
    title) must not refetch on every subsequent view."""
    engine, sm = await _mk_db(tmp_path, monkeypatch, "tagless")
    await dc.put("HARD-4", MovieDetail(code="HARD-4", title="t"))

    calls = {"n": 0}

    async def ok(cli, url, **kw):
        calls["n"] += 1
        return "<html>stub</html>"

    _wire_scraper(monkeypatch, ok)
    monkeypatch.setattr(
        jb, "_parse_detail", lambda html, code: MovieDetail(code=code, title="t")
    )

    await jb.fetch_detail("HARD-4")
    await jb.fetch_detail("HARD-4")
    assert calls["n"] == 1
    await engine.dispose()


async def test_refresh_failure_still_raises(tmp_path, monkeypatch):
    """Manual refresh keeps its error surface — the user asked for a
    refetch and must see that it failed."""
    engine, sm = await _mk_db(tmp_path, monkeypatch, "refresh")
    await dc.put("HARD-5", MovieDetail(code="HARD-5", title="t"))

    async def boom(cli, url, **kw):
        raise RuntimeError("javbus down")

    _wire_scraper(monkeypatch, boom)

    with pytest.raises(RuntimeError):
        await jb.fetch_detail("HARD-5", refresh=True)
    await engine.dispose()


async def test_backfill_breaker_aborts_after_consecutive_failures(monkeypatch):
    """A JavBus outage fails every code the same way — the cycle must
    abort instead of grinding through the whole batch."""
    import app.services.detail_backfill as db_mod

    codes = [f"BRK-{i}" for i in range(10)]

    async def fake_pick(limit):
        return codes

    calls = {"n": 0}

    async def boom(code):
        calls["n"] += 1
        raise RuntimeError("javbus down")

    monkeypatch.setattr(db_mod, "_pick_missing_codes", fake_pick)
    monkeypatch.setattr(db_mod.scraper, "fetch_detail_resolved", boom)
    monkeypatch.setattr(db_mod, "_attempted", set())
    monkeypatch.setattr(
        db_mod.settings, "actress_backfill_spacing_seconds", 0
    )

    fetched = await db_mod._backfill_details()
    assert fetched == 0
    assert calls["n"] == db_mod._BACKFILL_BREAKER_THRESHOLD  # not 10
    assert len(codes) > db_mod._BACKFILL_BREAKER_THRESHOLD


async def test_backfill_breaker_ignores_dead_codes(monkeypatch):
    """Empty-title results are JavBus answering 'does not exist' (dead
    codes cluster at the front of the pick order). They must NOT trip
    the outage breaker, or the batch never reaches the stale rows it
    exists to heal (2026-07-18 audit)."""
    import app.services.detail_backfill as db_mod
    from app.schemas import MovieDetail

    codes = [f"DEAD-{i}" for i in range(10)]

    async def fake_pick(limit):
        return codes

    calls = {"n": 0}

    async def empty(code):
        calls["n"] += 1
        return MovieDetail(code=code, title="")  # dead: JavBus 404

    monkeypatch.setattr(db_mod, "_pick_missing_codes", fake_pick)
    monkeypatch.setattr(db_mod.scraper, "fetch_detail_resolved", empty)
    monkeypatch.setattr(db_mod, "_attempted", set())
    monkeypatch.setattr(
        db_mod.settings, "actress_backfill_spacing_seconds", 0
    )

    await db_mod._backfill_details()
    assert calls["n"] == len(codes)  # walked the WHOLE batch, not just 5


async def test_put_logs_identity_drift(tmp_path, monkeypatch, caplog):
    """Overwriting a row with a different studio/series identity is
    logged — heal rewrites import today's JavBus identity over what the
    row held when the code was archived."""
    engine, sm = await _mk_db(tmp_path, monkeypatch, "drift")
    await dc.put(
        "DRIFT-1",
        MovieDetail(code="DRIFT-1", title="t", studio=LinkRef(name="OldStudio", id="s1")),
    )
    with caplog.at_level(logging.WARNING, logger="app.services.detail_cache"):
        await dc.put(
            "DRIFT-1",
            MovieDetail(
                code="DRIFT-1",
                title="t",
                studio=LinkRef(name="NewStudio", id="s2"),
                genres=[GenreRef(name="g", id="1")],
            ),
        )
    assert any("identity drift" in r.message for r in caplog.records)

    # Same identity → silent.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.services.detail_cache"):
        await dc.put(
            "DRIFT-1",
            MovieDetail(
                code="DRIFT-1",
                title="t",
                studio=LinkRef(name="NewStudio", id="s2"),
            ),
        )
    assert not any("identity drift" in r.message for r in caplog.records)
    await engine.dispose()
