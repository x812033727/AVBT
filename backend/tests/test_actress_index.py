"""Actress aggregation: presence ∩ detail-cache grouping for the 女優 page."""

import json
from datetime import datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.actress_index as ai
from app.database import Base
from app.models import ActressAvatar, MovieDetailCache


class FakePresence:
    def __init__(self, codes):
        self._codes = set(codes)

    def peek(self):
        return set(self._codes)

    async def get(self, *, force=False):
        return set(self._codes)


def _detail_row(code, title="t", cover="", release_date="", actresses=()):
    detail = {
        "code": code,
        "title": title,
        "cover": cover,
        "release_date": release_date,
        "actresses": [{"name": n, "id": i} for n, i in actresses],
        "genres": [],
        "samples": [],
        "magnets": [],
    }
    return MovieDetailCache(
        code=code,
        detail=json.dumps(detail),
        release_date=release_date,
        fetched_at=datetime.utcnow(),
    )


async def _seed(tmp_path, monkeypatch, rows, presence_codes, avatars=()):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/a.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(ai, "SessionLocal", maker)
    monkeypatch.setattr(ai, "presence_index", FakePresence(presence_codes))
    ai.invalidate()
    async with maker() as session:
        session.add_all(rows)
        session.add_all(
            ActressAvatar(id=i, name=n, avatar=av) for i, n, av in avatars
        )
        await session.commit()
    return engine


async def test_grouping_merges_id_and_idless_rows(tmp_path, monkeypatch):
    engine = await _seed(
        tmp_path,
        monkeypatch,
        [
            _detail_row("AAA-001", release_date="2024-01-01",
                        cover="c1", actresses=[("小島", "star1")]),
            _detail_row("AAA-002", release_date="2024-06-01",
                        cover="c2", actresses=[("小島", "")]),
            _detail_row("BBB-001", actresses=[("山田", "")]),
        ],
        presence_codes={"AAA-001", "AAA-002", "BBB-001"},
    )
    agg = await ai.get(force=True)
    await engine.dispose()

    assert set(agg.actresses) == {"小島", "山田"}
    kojima = agg.actresses["小島"]
    assert kojima.id == "star1"  # merged from the id-carrying row
    assert [w.code for w in kojima.works] == ["AAA-002", "AAA-001"]  # newest first
    assert kojima.sample_cover == "c2"
    assert agg.downloaded_total == 3
    assert agg.indexed_total == 3


async def test_only_downloaded_codes_counted(tmp_path, monkeypatch):
    engine = await _seed(
        tmp_path,
        monkeypatch,
        [
            _detail_row("AAA-001", actresses=[("小島", "star1")]),
            # Cached but NOT downloaded (e.g. browsed movie page) — excluded.
            _detail_row("ZZZ-999", actresses=[("路人", "star9")]),
        ],
        presence_codes={"AAA-001", "CCC-003"},  # CCC-003 downloaded, no detail yet
    )
    agg = await ai.get(force=True)
    await engine.dispose()

    assert set(agg.actresses) == {"小島"}
    assert agg.downloaded_total == 2
    assert agg.indexed_total == 1  # CCC-003 has no detail row yet


async def test_corrupt_row_and_empty_name_skipped(tmp_path, monkeypatch):
    bad = MovieDetailCache(code="BAD-001", detail="{not json", release_date="")
    engine = await _seed(
        tmp_path,
        monkeypatch,
        [
            bad,
            _detail_row("AAA-001", actresses=[("", "starX"), ("  ", ""), ("好人", "")]),
        ],
        presence_codes={"BAD-001", "AAA-001"},
    )
    agg = await ai.get(force=True)
    await engine.dispose()

    assert set(agg.actresses) == {"好人"}
    assert agg.indexed_total == 1  # corrupt row not counted as indexed


async def test_avatar_join_and_works_for(tmp_path, monkeypatch):
    engine = await _seed(
        tmp_path,
        monkeypatch,
        [_detail_row("AAA-001", actresses=[("小島", "star1")])],
        presence_codes={"AAA-001"},
        avatars=[("star1", "小島", "http://img/av.jpg"), ("starX", "他人", "")],
    )
    agg = await ai.get(force=True)
    assert agg.actresses["小島"].avatar == "http://img/av.jpg"

    entry = await ai.works_for("小島")
    assert entry is not None and entry.works[0].code == "AAA-001"
    assert await ai.works_for("不存在") is None
    await engine.dispose()


async def test_presence_failure_returns_empty(tmp_path, monkeypatch):
    class DeadPresence:
        def peek(self):
            return None

        async def get(self, *, force=False):
            raise RuntimeError("pikpak down")

    engine = await _seed(tmp_path, monkeypatch, [], set())
    monkeypatch.setattr(ai, "presence_index", DeadPresence())
    ai.invalidate()
    agg = await ai.get(force=True)
    await engine.dispose()
    assert agg.actresses == {} and agg.downloaded_total == 0
