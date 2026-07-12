"""Label→studio hard-cutover (generalized migrate_kind_to_studio)."""

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.tracking_migration as mig
from app.database import Base
from app.models import MovieDetailCache, TrackedListing


def _cache(code, label, studio):
    detail = {
        "code": code, "title": "t",
        "studio": {"name": studio[0], "id": studio[1]},
        "label": {"name": label[0], "id": label[1]},
        "actresses": [], "genres": [], "samples": [], "magnets": [],
    }
    return MovieDetailCache(
        code=code, detail=json.dumps(detail), release_date="",
        fetched_at=datetime.utcnow(),
    )


async def _setup(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/l.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(mig, "SessionLocal", maker)
    monkeypatch.setattr(mig, "engine", engine)
    async with maker() as s:
        s.add_all([
            TrackedListing(kind="label", id="9x", name="S1NO.1STYLE"),   # → 7q (new)
            TrackedListing(kind="label", id="81", name="MOODYZBest"),    # → 4v (already tracked)
            TrackedListing(kind="label", id="reb", name="REbecca"),      # no cache → unresolved
            TrackedListing(kind="studio", id="4v", name="ムーディーズ"),    # existing studio
            TrackedListing(kind="series", id="u0g", name="風俗タワー"),     # untouched by label run
            _cache("SONE-1", ("S1NO.1STYLE", "9x"), ("エスワン", "7q")),
            _cache("MIDE-1", ("MOODYZBest", "81"), ("ムーディーズ", "4v")),
        ])
        await s.commit()
    return engine, maker


async def test_label_dry_run(tmp_path, monkeypatch):
    engine, maker = await _setup(tmp_path, monkeypatch)
    r = await mig.migrate_kind_to_studio("label", dry_run=True)
    assert r["source_kind"] == "label"
    assert r["source_count"] == 3
    assert {s["id"] for s in r["studios"]} == {"7q", "4v"}
    assert r["already_tracked_studios"] == ["4v"]        # 81→4v already tracked
    assert [u["id"] for u in r["unresolved"]] == ["reb"]
    assert r["source_deleted"] == 0
    async with maker() as s:
        labels = (await s.execute(select(TrackedListing).where(TrackedListing.kind == "label"))).scalars().all()
    assert len(labels) == 3                              # nothing mutated
    await engine.dispose()


async def test_label_real_run(tmp_path, monkeypatch):
    engine, maker = await _setup(tmp_path, monkeypatch)
    r = await mig.migrate_kind_to_studio("label", dry_run=False, auto_send=True)
    assert r["source_deleted"] == 3
    assert r["studios_added"] == 1                       # only 7q new (4v already tracked)
    async with maker() as s:
        labels = (await s.execute(select(TrackedListing).where(TrackedListing.kind == "label"))).scalars().all()
        studios = (await s.execute(select(TrackedListing).where(TrackedListing.kind == "studio"))).scalars().all()
        series = (await s.execute(select(TrackedListing).where(TrackedListing.kind == "series"))).scalars().all()
    assert labels == []                                  # all labels removed
    assert {st.id for st in studios} == {"4v", "7q"}     # existing 4v kept, 7q added
    assert next(st for st in studios if st.id == "7q").auto_send is True
    assert len(series) == 1                              # series kind untouched
    await engine.dispose()


async def test_label_and_series_flags_independent(tmp_path, monkeypatch):
    engine, maker = await _setup(tmp_path, monkeypatch)
    # Run label migration for real, then a second label run is a no-op…
    await mig.migrate_kind_to_studio("label", dry_run=False)
    r2 = await mig.migrate_kind_to_studio("label", dry_run=False)
    assert r2["already_migrated"] is True and r2["source_deleted"] == 0
    # …but the series flag is separate, so a series run still proceeds.
    r3 = await mig.migrate_kind_to_studio("series", dry_run=False)
    assert r3["already_migrated"] is False
    async with maker() as s:
        series = (await s.execute(select(TrackedListing).where(TrackedListing.kind == "series"))).scalars().all()
    assert series == []                                  # series u0g resolved+deleted
    await engine.dispose()


async def test_invalid_from_kind_rejected(tmp_path, monkeypatch):
    engine, maker = await _setup(tmp_path, monkeypatch)
    import pytest
    with pytest.raises(ValueError):
        await mig.migrate_kind_to_studio("director", dry_run=True)
    await engine.dispose()
