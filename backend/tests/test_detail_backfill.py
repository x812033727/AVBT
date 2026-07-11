"""Backfill worker: picks only missing work, respects limits, records state."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.detail_backfill as bf
from app.database import Base
from app.models import ActressAvatar, MovieDetailCache
from app.schemas import MovieDetail


class FakePresence:
    def __init__(self, codes):
        self._codes = set(codes)

    def peek(self):
        return set(self._codes)

    async def get(self, *, force=False):
        return set(self._codes)


async def _setup(tmp_path, monkeypatch, *, presence, cached=()):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/b.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(bf, "SessionLocal", maker)
    monkeypatch.setattr(bf, "presence_index", FakePresence(presence))
    monkeypatch.setattr(bf.settings, "actress_backfill_spacing_seconds", 0.0)
    monkeypatch.setattr(bf, "state", bf.BackfillState())
    monkeypatch.setattr(bf, "_attempted", set())
    async with maker() as session:
        for code in cached:
            session.add(
                MovieDetailCache(code=code, detail="{}", release_date="")
            )
        await session.commit()
    return engine, maker


async def test_pick_missing_excludes_cached_and_attempted(tmp_path, monkeypatch):
    engine, _ = await _setup(
        tmp_path, monkeypatch,
        presence={"AAA-001", "AAA-002", "AAA-003", "AAA-004"},
        cached={"AAA-002"},
    )
    bf._attempted.add("AAA-003")

    picked = await bf._pick_missing_codes(2)
    await engine.dispose()
    assert picked == ["AAA-001", "AAA-004"]
    # pending reports the true gap (presence − cached), attempted included.
    assert bf.state.pending == 3


async def test_run_details_counts_and_skips_empty_titles(tmp_path, monkeypatch):
    engine, _ = await _setup(
        tmp_path, monkeypatch, presence={"AAA-001", "BBB-001"}, cached=set()
    )
    monkeypatch.setattr(bf.settings, "actress_backfill_batch_limit", 10)
    calls = []

    async def fake_fetch(code, *, refresh=False):
        calls.append(code)
        title = "ok" if code == "AAA-001" else ""
        return MovieDetail(code=code, title=title)

    monkeypatch.setattr(bf.scraper, "fetch_detail_resolved", fake_fetch)

    fetched = await bf._backfill_details()
    assert fetched == 1
    assert bf.state.done_total == 1 and bf.state.failed_total == 1

    # Second cycle: both codes are in _attempted now — nothing re-fetched.
    calls.clear()
    fetched = await bf._backfill_details()
    await engine.dispose()
    assert fetched == 0 and calls == []


async def test_avatar_pick_and_store(tmp_path, monkeypatch):
    engine, maker = await _setup(tmp_path, monkeypatch, presence=set())
    monkeypatch.setattr(bf.settings, "actress_avatar_batch_limit", 10)

    async def fake_agg(*, force=False):
        return SimpleNamespace(
            actresses={
                "A": SimpleNamespace(id="star1", name="A"),
                "B": SimpleNamespace(id="star2", name="B"),
                "C": SimpleNamespace(id="", name="C"),        # no id → never picked
                "D": SimpleNamespace(id="star3", name="D"),   # fresh negative row
            }
        )

    monkeypatch.setattr(bf.actress_index, "get", fake_agg)
    async with maker() as session:
        session.add(
            ActressAvatar(id="star3", name="D", avatar="", fetched_at=datetime.utcnow())
        )
        # Stale negative row → eligible for retry.
        session.add(
            ActressAvatar(
                id="star2", name="B", avatar="",
                fetched_at=datetime.utcnow() - timedelta(days=40),
            )
        )
        await session.commit()

    async def fake_profile(star_id, *, uncensored=False):
        return SimpleNamespace(avatar=f"http://img/{star_id}.jpg")

    monkeypatch.setattr(bf.scraper, "fetch_star_profile", fake_profile)

    fetched = await bf._backfill_avatars()
    assert fetched == 2  # star1 (new) + star2 (stale negative)
    async with maker() as session:
        rows = dict(
            (await session.execute(select(ActressAvatar.id, ActressAvatar.avatar))).all()
        )
    await engine.dispose()
    assert rows["star1"] == "http://img/star1.jpg"
    assert rows["star2"] == "http://img/star2.jpg"
    assert rows["star3"] == ""  # fresh negative untouched


async def test_avatar_failure_stores_negative_marker(tmp_path, monkeypatch):
    engine, maker = await _setup(tmp_path, monkeypatch, presence=set())
    monkeypatch.setattr(bf.settings, "actress_avatar_batch_limit", 10)

    async def fake_agg(*, force=False):
        return SimpleNamespace(actresses={"A": SimpleNamespace(id="star1", name="A")})

    async def boom(star_id, *, uncensored=False):
        raise RuntimeError("429")

    monkeypatch.setattr(bf.actress_index, "get", fake_agg)
    monkeypatch.setattr(bf.scraper, "fetch_star_profile", boom)

    fetched = await bf._backfill_avatars()
    assert fetched == 0
    async with maker() as session:
        row = (
            await session.execute(select(ActressAvatar).where(ActressAvatar.id == "star1"))
        ).scalar_one()
    await engine.dispose()
    assert row.avatar == ""  # negative marker prevents per-cycle refetch


def test_detail_row_json_shape_matches_schema():
    # Guard: the aggregation parses rows with MovieDetail.model_validate_json.
    d = MovieDetail(code="X-1", title="t")
    parsed = MovieDetail.model_validate_json(json.dumps(json.loads(d.model_dump_json())))
    assert parsed.code == "X-1"
