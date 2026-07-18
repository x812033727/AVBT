"""Missing-scan ignore list: a code on the list counts neither as
missing (even though the catalog lists it and it's not present) nor as
an extra (even though it's physically on disk under the listing's
folder but absent from the catalog). Persisted in AppMeta as JSON so it
survives restarts, same storage pattern as presence's _BUILT_AT_KEY."""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as db
from app.models import TrackedListing
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


def _stub_listing(monkeypatch, items):
    async def fake_fetch(kind, slug, *, uncensored, refresh=False, **kw):
        return (list(items), 1)

    monkeypatch.setattr(missing_svc, "fetch_all_listing_codes", fake_fetch, raising=True)

    async def no_owned(*a, **kw):
        return []

    monkeypatch.setattr(missing_svc, "_owned_listing_members", no_owned, raising=True)


def _stub_presence(monkeypatch, present: set[str], extras: dict[str, list[str]]):
    async def fake_get(*, force: bool = False):
        return set(present)

    monkeypatch.setattr(missing_svc.presence_index, "get", fake_get, raising=True)

    def fake_codes_under(*prefixes):
        return dict(extras)

    monkeypatch.setattr(
        missing_svc.presence_index, "codes_under", fake_codes_under, raising=True
    )


async def test_ignored_code_excluded_from_missing_and_extras(tmp_path, monkeypatch):
    await _bind_tmp_db(tmp_path, monkeypatch)
    # ABC-001 is present, ABC-002 is missing, ABC-099 sits on disk under
    # the folder but isn't in the catalog at all (would-be extra).
    _stub_listing(monkeypatch, [_item("ABC-001"), _item("ABC-002")])
    _stub_presence(
        monkeypatch, {"ABC-001"}, {"ABC-099": ["AVBT/製作商/abc/ABC-099"]}
    )

    await missing_svc.add_ignored_code("ABC-002", "已知無素材")
    await missing_svc.add_ignored_code("ABC-099", "誤判")

    res = await missing_svc.missing_for_listing("studio", "abc", dedup=False)

    assert [m.code for m in res.missing] == []
    assert [e.code for e in res.extras] == []


async def test_ignored_code_reappears_after_delete(tmp_path, monkeypatch):
    await _bind_tmp_db(tmp_path, monkeypatch)
    _stub_listing(monkeypatch, [_item("ABC-001"), _item("ABC-002")])
    _stub_presence(monkeypatch, {"ABC-001"}, {})

    await missing_svc.add_ignored_code("ABC-002", "temp")
    res1 = await missing_svc.missing_for_listing("studio", "abc", dedup=False)
    assert [m.code for m in res1.missing] == []

    await missing_svc.remove_ignored_code("ABC-002")
    res2 = await missing_svc.missing_for_listing("studio", "abc", dedup=False)
    assert [m.code for m in res2.missing] == ["ABC-002"]


async def test_add_ignored_code_normalizes(tmp_path, monkeypatch):
    await _bind_tmp_db(tmp_path, monkeypatch)

    await missing_svc.add_ignored_code("abc-002", "lowercase input")

    ignored = await missing_svc.get_ignored_codes()
    assert ignored == {"ABC-002": "lowercase input"}


async def test_remove_ignored_code_normalizes(tmp_path, monkeypatch):
    await _bind_tmp_db(tmp_path, monkeypatch)

    await missing_svc.add_ignored_code("ABC-002", "x")
    await missing_svc.remove_ignored_code("abc-002")

    assert await missing_svc.get_ignored_codes() == {}


async def test_ignored_codes_persist_across_reads(tmp_path, monkeypatch):
    await _bind_tmp_db(tmp_path, monkeypatch)

    await missing_svc.add_ignored_code("ABC-003", "r1")
    await missing_svc.add_ignored_code("ABC-004", "r2")

    ignored = await missing_svc.get_ignored_codes()
    assert ignored == {"ABC-003": "r1", "ABC-004": "r2"}


async def test_extras_still_show_when_not_ignored(tmp_path, monkeypatch):
    # Sanity check for the fixture harness itself: without an ignore
    # entry the extra must still surface, so test_ignored_code_excluded_*
    # above is actually proving something.
    await _bind_tmp_db(tmp_path, monkeypatch)
    _stub_listing(monkeypatch, [_item("ABC-001")])
    _stub_presence(
        monkeypatch, {"ABC-001"}, {"ABC-099": ["AVBT/製作商/abc/ABC-099"]}
    )

    res = await missing_svc.missing_for_listing("studio", "abc", dedup=False)

    assert [e.code for e in res.extras] == ["ABC-099"]


async def test_missing_summary_threads_ignored_set_through_summary_item(
    tmp_path, monkeypatch
):
    # missing_summary rebuilds via _parallel_map(_summary_item, ...) with
    # ``ignored`` loaded once and passed down — cover that wiring, not
    # just the single-listing missing_for_listing path above.
    await _bind_tmp_db(tmp_path, monkeypatch)
    # Defensive: missing_summary caches into module globals — make sure
    # this test starts from a clean slate regardless of run order.
    monkeypatch.setattr(missing_svc, "_summary_result", None)
    _stub_listing(monkeypatch, [_item("ABC-001"), _item("ABC-002")])
    _stub_presence(monkeypatch, {"ABC-001"}, {})

    maker = missing_svc.SessionLocal
    async with maker() as s:
        s.add(TrackedListing(kind="studio", id="abc", name="ABC"))
        await s.commit()

    await missing_svc.add_ignored_code("ABC-002", "known-empty")

    summary = await missing_svc.missing_summary(refresh=True)

    assert len(summary.items) == 1
    assert summary.items[0].missing_count == 0
