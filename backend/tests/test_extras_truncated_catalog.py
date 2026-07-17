"""Extras are unjudgeable when the catalog walk hit the page cap.

Big studios exceed ``missing_max_pages`` (~1,500 works), so every owned
work older than the cap window used to be flagged 多餘 (live: 2,060
false extras across 8 studios, 2026-07-17). A truncated catalog now
yields no extras and sets ``catalog_truncated`` for the UI to explain.
"""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as db
from app.config import settings
from app.services import missing as missing_svc
from app.services.missing import MovieListItem


async def _bind_tmp_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", maker)
    monkeypatch.setattr(missing_svc, "SessionLocal", maker)


def _item(code: str) -> MovieListItem:
    return MovieListItem(code=code, title=code)


def _stub(monkeypatch, *, pages: int, extra_code: str = "ZZZ-999"):
    async def fake_fetch(kind, slug, *, uncensored, refresh=False, **kw):
        return ([_item("ABC-001")], pages)

    monkeypatch.setattr(missing_svc, "fetch_all_listing_codes", fake_fetch)

    async def no_owned(*a, **kw):
        return []

    monkeypatch.setattr(missing_svc, "_owned_listing_members", no_owned)

    async def fake_presence(*, force: bool = False):
        return {"ABC-001", extra_code}

    monkeypatch.setattr(missing_svc.presence_index, "get", fake_presence)
    monkeypatch.setattr(
        missing_svc.presence_index,
        "codes_under",
        lambda *roots: {extra_code: ["AVBT/製作商/x/未分類/f.mp4"]},
    )


async def test_capped_walk_suppresses_extras(tmp_path, monkeypatch):
    await _bind_tmp_db(tmp_path, monkeypatch)
    _stub(monkeypatch, pages=settings.missing_max_pages)

    res = await missing_svc.missing_for_listing("studio", "big", dedup=False)

    assert res.catalog_truncated is True
    assert res.extras == [], "capped catalog → extras unjudgeable"


async def test_full_walk_still_reports_extras(tmp_path, monkeypatch):
    await _bind_tmp_db(tmp_path, monkeypatch)
    _stub(monkeypatch, pages=3)

    res = await missing_svc.missing_for_listing("studio", "small", dedup=False)

    assert res.catalog_truncated is False
    assert [e.code for e in res.extras] == ["ZZZ-999"]
