"""``refresh`` means "re-fetch the JavBus listing", never "re-walk PikPak".

Both flags used to ride on one parameter: ``missing_for_listing(refresh=True)``
passed it straight into ``presence_index.get(force=refresh)``, so every caller
that just wanted a fresh JavBus catalog also bought a full drive walk (~2.5min
of PikPak calls). Since the index is persisted and the pipeline refreshes each
code as it lands (#163), that walk is pure load — and the rotation's top-up
tool hits this endpoint every round.
"""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as db
from app.services import missing as missing_svc
from app.services.missing import MovieListItem


async def _bind_tmp_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", maker)
    # missing binds SessionLocal at import time — patch its own reference.
    monkeypatch.setattr(missing_svc, "SessionLocal", maker)


def _item(code: str) -> MovieListItem:
    return MovieListItem(code=code, title=code)


def _stub_listing(monkeypatch, seen: list[bool]):
    async def fake_fetch(kind, slug, *, uncensored, refresh=False, **kw):
        seen.append(refresh)
        return ([_item("ABC-001"), _item("ABC-002")], 1)

    monkeypatch.setattr(
        missing_svc, "fetch_all_listing_codes", fake_fetch, raising=True
    )

    async def no_owned(*a, **kw):
        return []

    monkeypatch.setattr(
        missing_svc, "_owned_listing_members", no_owned, raising=True
    )


def _stub_presence(monkeypatch, forces: list[bool]):
    async def fake_get(*, force: bool = False):
        forces.append(force)
        return {"ABC-001"}

    monkeypatch.setattr(
        missing_svc.presence_index, "get", fake_get, raising=True
    )


async def test_refresh_refetches_listing_without_presence_full_walk(
    tmp_path, monkeypatch
):
    await _bind_tmp_db(tmp_path, monkeypatch)
    listing_refresh: list[bool] = []
    presence_force: list[bool] = []
    _stub_listing(monkeypatch, listing_refresh)
    _stub_presence(monkeypatch, presence_force)

    res = await missing_svc.missing_for_listing(
        "studio", "abc", refresh=True, dedup=False
    )

    assert listing_refresh == [True]  # JavBus catalog still re-fetched
    assert presence_force == [False]  # ...but no full drive walk
    assert [m.code for m in res.missing] == ["ABC-002"]


async def test_default_call_also_avoids_full_walk(tmp_path, monkeypatch):
    await _bind_tmp_db(tmp_path, monkeypatch)
    listing_refresh: list[bool] = []
    presence_force: list[bool] = []
    _stub_listing(monkeypatch, listing_refresh)
    _stub_presence(monkeypatch, presence_force)

    await missing_svc.missing_for_listing("studio", "abc", dedup=False)

    assert listing_refresh == [False]
    assert presence_force == [False]
