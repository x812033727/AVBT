"""refresh_codes must list a shared archive folder once per call.

Each code's _live_paths_for lists the shared legacy folder AVBT/已完成
(plus its studio/series dir). Without a per-call memo, K codes trigger K
listings of the SAME legacy folder. A request-scoped, concurrency-
coalescing memo collapses that to one listing while keeping results
identical — and never caches across calls (no staleness).
"""

import asyncio
from types import SimpleNamespace

import pytest

import app.services.archiver as archiver_mod
import app.services.pikpak_presence as pp
from app.services.pikpak_presence import _ListingMemo


async def test_listing_memo_coalesces_concurrent_calls():
    calls = {"n": 0}
    started = asyncio.Event()

    async def loader(parent_id):
        calls["n"] += 1
        started.set()
        await asyncio.sleep(0)  # yield so the second caller races in
        return [f"item-of-{parent_id}"]

    memo = _ListingMemo(loader)
    a, b = await asyncio.gather(memo.get("F"), memo.get("F"))
    assert a == b == ["item-of-F"]
    assert calls["n"] == 1        # one load despite two concurrent gets


async def test_listing_memo_does_not_cache_failure():
    calls = {"n": 0}

    async def loader(parent_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return ["ok"]

    memo = _ListingMemo(loader)
    with pytest.raises(RuntimeError):
        await memo.get("F")
    assert await memo.get("F") == ["ok"]   # retried, not a cached failure
    assert calls["n"] == 2


async def test_listing_memo_does_not_cache_empty():
    calls = {"n": 0}

    async def loader(parent_id):
        calls["n"] += 1
        return []  # genuine-empty OR a swallowed transient failure

    memo = _ListingMemo(loader)
    assert await memo.get("F") == []
    assert await memo.get("F") == []
    assert calls["n"] == 2   # empty not cached → re-listed each time


async def test_refresh_codes_lists_shared_folder_once(monkeypatch):
    index = pp.PikPakPresenceIndex()
    index._codes = set()
    index._paths = {}

    async def noop_persist(code, paths):
        return None

    monkeypatch.setattr(index, "_persist_code", noop_persist)

    # Only the legacy folder is searched (nested studio/series dir is None).
    async def no_nested(code, *, allow_fetch=False):
        return None

    monkeypatch.setattr(archiver_mod, "studio_series_dir_for_code", no_nested)

    async def fake_lookup(path):
        return "leg" if path.strip("/") == "AVBT/已完成" else None

    list_calls = {"leg": 0}

    async def fake_list_all(parent_id, *, cap):
        list_calls[parent_id] = list_calls.get(parent_id, 0) + 1
        return [
            SimpleNamespace(id="a", name="ABC-001"),
            SimpleNamespace(id="b", name="ABC-002"),
            SimpleNamespace(id="c", name="ABC-003"),
        ], False

    monkeypatch.setattr(pp.pikpak_service, "lookup_folder_id", fake_lookup)
    monkeypatch.setattr(pp.pikpak_service, "list_all_files", fake_list_all)
    monkeypatch.setattr(pp.settings, "pikpak_archive_folder", "AVBT/已完成")

    changed = await index.refresh_codes(["ABC-001", "ABC-002", "ABC-003"])

    assert changed == 3
    assert index._paths["ABC-001"] == ["AVBT/已完成/ABC-001"]
    assert list_calls["leg"] == 1   # listed ONCE for all three codes
