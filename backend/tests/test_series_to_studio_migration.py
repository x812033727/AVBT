"""Series→studio hard-cutover migration (dry-run + real run + idempotent)."""

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.tracking_migration as mig
from app.database import Base
from app.models import MovieDetailCache, TrackedListing


def _series(id, name):
    return TrackedListing(kind="series", id=id, name=name)


def _cache(code, series, studio):
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


async def _setup(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/m.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(mig, "SessionLocal", maker)
    monkeypatch.setattr(mig, "engine", engine)
    async with maker() as s:
        s.add_all([
            _series("u0g", "風俗タワー"),
            _series("ts9", "天然成分由来"),
            _series("orphan", "無快取系列"),          # no cache → unresolved
            TrackedListing(kind="studio", id="c2", name="既有製作商"),
            TrackedListing(kind="label", id="9x", name="S1"),
            _cache("YRK-1", ("風俗タワー", "u0g"), ("プレステージ", "75")),
            _cache("YRK-2", ("天然成分由来", "ts9"), ("プレステージ", "75")),
            _cache("MIDE-1", ("天然成分由来", "ts9"), ("ムーディーズ", "4v")),  # minority
        ])
        await s.commit()
    return engine, maker


async def test_dry_run_reports_without_mutating(tmp_path, monkeypatch):
    engine, maker = await _setup(tmp_path, monkeypatch)
    rep = await mig.migrate_series_to_studio(dry_run=True)
    assert rep["dry_run"] is True
    assert rep["series_count"] == 3
    # u0g→75, ts9→75 (dominant, 2 vs 1) → distinct {75}
    assert {s["id"] for s in rep["studios"]} == {"75"}
    assert [u["id"] for u in rep["unresolved_series"]] == ["orphan"]
    assert rep["series_deleted"] == 0
    # nothing mutated
    async with maker() as s:
        n = (await s.execute(select(TrackedListing).where(TrackedListing.kind == "series"))).scalars().all()
    assert len(n) == 3
    await engine.dispose()


async def test_real_run_cuts_over_and_backs_up(tmp_path, monkeypatch):
    engine, maker = await _setup(tmp_path, monkeypatch)
    rep = await mig.migrate_series_to_studio(dry_run=False, auto_send=True)
    assert rep["series_deleted"] == 3
    assert rep["studios_added"] == 1          # 75 added; c2 already existed & unrelated
    assert rep["backup_path"] and rep["backup_path"].endswith(tuple("0123456789"))

    async with maker() as s:
        series = (await s.execute(select(TrackedListing).where(TrackedListing.kind == "series"))).scalars().all()
        studios = (await s.execute(select(TrackedListing).where(TrackedListing.kind == "studio"))).scalars().all()
        labels = (await s.execute(select(TrackedListing).where(TrackedListing.kind == "label"))).scalars().all()
    assert series == []
    assert {st.id for st in studios} == {"75", "c2"}     # existing c2 preserved
    added = next(st for st in studios if st.id == "75")
    assert added.auto_send is True and added.name == "プレステージ"
    assert len(labels) == 1                              # 發行商 untouched
    await engine.dispose()


async def test_real_run_is_idempotent(tmp_path, monkeypatch):
    engine, maker = await _setup(tmp_path, monkeypatch)
    await mig.migrate_series_to_studio(dry_run=False)
    # re-add a stray series to prove the flag blocks a second cutover
    async with maker() as s:
        s.add(_series("late", "後來才追的"))
        await s.commit()
    rep2 = await mig.migrate_series_to_studio(dry_run=False)
    assert rep2["already_migrated"] is True
    assert rep2["series_deleted"] == 0
    async with maker() as s:
        series = (await s.execute(select(TrackedListing).where(TrackedListing.kind == "series"))).scalars().all()
    assert {r.id for r in series} == {"late"}           # not deleted on second run
    await engine.dispose()
