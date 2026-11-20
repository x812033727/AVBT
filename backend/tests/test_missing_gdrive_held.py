"""Works moved to Google Drive are held, not 缺漏.

A Drive move with 搬完刪 PikPak trashes the PikPak source and the presence
index drops the code. ``missing_for_listing`` judged on presence alone, so
the moved work came back as missing and the tracker's backfill (which sends
whatever this function calls missing) would download it again.
"""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as db
from app.models import GDriveLocation
from app.services import missing as missing_svc
from app.services.missing import MovieListItem


async def _bind_tmp_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", maker)
    monkeypatch.setattr(missing_svc, "SessionLocal", maker)
    return maker


def _stub(monkeypatch, codes: list[str], presence: set[str]):
    async def fake_fetch(kind, slug, *, uncensored, refresh=False, **kw):
        return ([MovieListItem(code=c, title=c) for c in codes], 1)

    async def no_owned(*a, **kw):
        return []

    async def fake_get(*, force: bool = False):
        return presence

    monkeypatch.setattr(missing_svc, "fetch_all_listing_codes", fake_fetch)
    monkeypatch.setattr(missing_svc, "_owned_listing_members", no_owned)
    monkeypatch.setattr(missing_svc.presence_index, "get", fake_get)


async def _add_location(maker, code: str, *, is_video: bool = True):
    async with maker() as session:
        session.add(
            GDriveLocation(
                code=code,
                account_id=1,
                job_id=1,
                studio_id="abc",
                drive_file_id=f"f-{code}-{is_video}",
                path=f"AVBT/製作商/X/Y/{code}.mp4",
                name=f"{code}.mp4",
                size=1,
                is_video=is_video,
            )
        )
        await session.commit()


async def test_moved_to_drive_is_not_missing(tmp_path, monkeypatch):
    maker = await _bind_tmp_db(tmp_path, monkeypatch)
    presence = {"ABC-001"}
    _stub(monkeypatch, ["ABC-001", "ABC-002", "ABC-003"], presence)
    await _add_location(maker, "ABC-002")

    res = await missing_svc.missing_for_listing("studio", "abc")

    assert [m.code for m in res.missing] == ["ABC-003"]
    assert sorted(res.present_codes) == ["ABC-001", "ABC-002"]
    # The index's own set must not be mutated by the union.
    assert presence == {"ABC-001"}


async def test_non_video_drive_file_does_not_count(tmp_path, monkeypatch):
    maker = await _bind_tmp_db(tmp_path, monkeypatch)
    _stub(monkeypatch, ["ABC-001", "ABC-002"], {"ABC-001"})
    await _add_location(maker, "ABC-002", is_video=False)

    res = await missing_svc.missing_for_listing("studio", "abc")

    assert [m.code for m in res.missing] == ["ABC-002"]


async def test_no_drive_rows_is_plain_presence(tmp_path, monkeypatch):
    await _bind_tmp_db(tmp_path, monkeypatch)
    _stub(monkeypatch, ["ABC-001", "ABC-002"], {"ABC-001"})

    res = await missing_svc.missing_for_listing("studio", "abc")

    assert [m.code for m in res.missing] == ["ABC-002"]
