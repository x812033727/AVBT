"""Studio aggregation: presence ∩ detail-cache grouping for the 製作商 page."""

import json
from datetime import datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.studio_index as si
from app.database import Base
from app.models import MovieDetailCache


class FakePresence:
    def __init__(self, codes):
        self._codes = set(codes)

    def peek(self):
        return set(self._codes)

    async def get(self, *, force=False):
        return set(self._codes)


def _detail_row(code, title="t", cover="", release_date="", studio=None, series=None):
    detail = {
        "code": code,
        "title": title,
        "cover": cover,
        "release_date": release_date,
        "studio": {"name": studio[0], "id": studio[1]} if studio else None,
        "series": {"name": series[0], "id": series[1]} if series else None,
        "actresses": [],
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


async def _seed(tmp_path, monkeypatch, rows, presence_codes):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/s.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(si, "SessionLocal", maker)
    monkeypatch.setattr(si, "presence_index", FakePresence(presence_codes))
    si.invalidate()
    async with maker() as session:
        session.add_all(rows)
        await session.commit()
    return engine


async def test_studio_series_grouping_newest_first(tmp_path, monkeypatch):
    engine = await _seed(
        tmp_path,
        monkeypatch,
        [
            _detail_row("PRE-001", release_date="2024-01-01", cover="c1",
                        studio=("プレステージ", "75"), series=("風俗タワー", "u0g")),
            _detail_row("PRE-002", release_date="2024-06-01", cover="c2",
                        studio=("プレステージ", "75"), series=("風俗タワー", "u0g")),
            _detail_row("PRE-003", release_date="2023-01-01", cover="c3",
                        studio=("プレステージ", "75"), series=("天然成分由来", "ts9")),
        ],
        presence_codes={"PRE-001", "PRE-002", "PRE-003"},
    )
    agg = await si.get(force=True)
    await engine.dispose()

    assert set(agg.studios) == {"75"}
    studio = agg.studios["75"]
    assert studio.name == "プレステージ"
    assert studio.work_count == 3
    assert set(studio.series) == {"u0g", "ts9"}
    tower = studio.series["u0g"]
    assert [w.code for w in tower.works] == ["PRE-002", "PRE-001"]  # newest first
    assert tower.sample_cover == "c2"
    assert agg.indexed_total == 3


async def test_no_series_bucket(tmp_path, monkeypatch):
    engine = await _seed(
        tmp_path,
        monkeypatch,
        [
            _detail_row("X-1", studio=("ムーディーズ", "4v"), series=None),
            _detail_row("X-2", studio=("ムーディーズ", "4v"), series=("", "")),
        ],
        presence_codes={"X-1", "X-2"},
    )
    agg = await si.get(force=True)
    await engine.dispose()

    studio = agg.studios["4v"]
    assert set(studio.series) == {si.NO_SERIES}
    assert studio.series[si.NO_SERIES].name == si.NO_SERIES_NAME
    assert len(studio.series[si.NO_SERIES].works) == 2


async def test_no_studio_excluded(tmp_path, monkeypatch):
    engine = await _seed(
        tmp_path,
        monkeypatch,
        [
            _detail_row("A-1", studio=("プレステージ", "75"), series=("s", "s1")),
            _detail_row("B-1", studio=None, series=("s", "s1")),  # no studio → skipped
        ],
        presence_codes={"A-1", "B-1"},
    )
    agg = await si.get(force=True)
    await engine.dispose()

    assert set(agg.studios) == {"75"}
    assert agg.downloaded_total == 2
    assert agg.indexed_total == 1  # B-1 has no studio


async def test_only_downloaded_counted_and_lookups(tmp_path, monkeypatch):
    engine = await _seed(
        tmp_path,
        monkeypatch,
        [
            _detail_row("A-1", studio=("プレステージ", "75"), series=("塔", "u0g")),
            _detail_row("Z-9", studio=("他社", "zz"), series=("x", "x1")),  # cached, not downloaded
        ],
        presence_codes={"A-1"},
    )
    agg = await si.get(force=True)

    assert set(agg.studios) == {"75"}
    entry = await si.studio_for("75")
    assert entry is not None and entry.work_count == 1
    pair = await si.series_for("75", "u0g")
    assert pair is not None and pair[1].works[0].code == "A-1"
    assert await si.series_for("75", "nope") is None
    assert await si.studio_for("nope") is None
    await engine.dispose()


async def test_presence_failure_returns_empty(tmp_path, monkeypatch):
    class DeadPresence:
        def peek(self):
            return None

        async def get(self, *, force=False):
            raise RuntimeError("pikpak down")

    engine = await _seed(tmp_path, monkeypatch, [], set())
    monkeypatch.setattr(si, "presence_index", DeadPresence())
    si.invalidate()
    agg = await si.get(force=True)
    await engine.dispose()
    assert agg.studios == {} and agg.downloaded_total == 0
