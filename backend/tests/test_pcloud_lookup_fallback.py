"""lookup_path / ensure_path tolerate the same drifted-name twins PikPak's
lookup_folder_id does (folder_key: spacing / width / case), plus the
trailing-ASCII-dot case #210 introduced: safe_folder_name now rstrips
ASCII dots before creating new pCloud folders, so a folder created before
#210 under the dotted name (e.g. "働くドMさん.") is a legacy twin of the
new dot-stripped lookup name ("働くドMさん"). Without a fallback, the exact
``c.name == seg`` match misses it and ensure_path forks a sibling,
splitting the series on the pCloud mirror — same failure mode #163/live
2026-07-16 hit on PikPak before ``folder_key`` was added there.
"""

from app.schemas import PCloudFile
from app.services.pcloud import PCloudService


def _folder(id_, name):
    return PCloudFile(id=str(id_), name=name, kind="folder")


def _fake_list_files(children_by_parent):
    async def list_files(parent_id="0", size=0):
        return children_by_parent.get(str(parent_id), [])
    return list_files


# ---------- lookup_path ----------

async def test_lookup_path_exact_match_preferred():
    svc = PCloudService()
    # Both an exact match AND a folder_key-equal twin exist, twin listed
    # FIRST — a single-pass "check exact-then-twin per element" scan would
    # return the twin here; exact must win regardless of listing order.
    svc.list_files = _fake_list_files({
        "0": [_folder(2, "abc"), _folder(1, "ABC")],
    })
    assert await svc.lookup_path("ABC") == 1


async def test_lookup_path_reuses_trailing_dot_twin():
    svc = PCloudService()
    # Legacy folder kept its ASCII dot; #210 makes new lookups ask for the
    # dot-stripped name. lookup_path must still find the old folder.
    svc.list_files = _fake_list_files({
        "0": [_folder(7, "働くドMさん.")],
    })
    assert await svc.lookup_path("働くドMさん") == 7


async def test_lookup_path_reuses_width_and_case_twin():
    svc = PCloudService()
    svc.list_files = _fake_list_files({
        "0": [_folder(3, "新人NO.1 STYLE")],
    })
    assert await svc.lookup_path("新人NO.1STYLE") == 3


async def test_lookup_path_does_not_merge_different_names():
    svc = PCloudService()
    # "ABC" vs "ABCD" share no folder_key equivalence — must stay distinct
    # (and here ABCD doesn't exist at all, so this is a miss, not a merge).
    svc.list_files = _fake_list_files({
        "0": [_folder(1, "ABC")],
    })
    assert await svc.lookup_path("ABCD") is None


async def test_lookup_path_nested_segments_use_fallback_at_each_level():
    svc = PCloudService()
    svc.list_files = _fake_list_files({
        "0": [_folder(1, "Studio.")],
        "1": [_folder(2, "Series")],
    })
    assert await svc.lookup_path("Studio/Series") == 2


# ---------- ensure_path ----------

async def test_ensure_path_reuses_trailing_dot_twin_without_creating(monkeypatch):
    svc = PCloudService()
    svc.list_files = _fake_list_files({
        "0": [_folder(7, "働くドMさん.")],
    })
    calls = []

    async def fake_call(method, params=None):
        calls.append((method, params))
        raise AssertionError(f"unexpected pCloud call: {method}({params})")

    svc._call = fake_call

    result = await svc.ensure_path("働くドMさん")
    assert result == 7
    assert calls == []  # no createfolderifnotexists — twin was reused


async def test_ensure_path_creates_when_no_twin_exists(monkeypatch):
    svc = PCloudService()
    svc.list_files = _fake_list_files({"0": []})
    calls = []

    async def fake_call(method, params=None):
        calls.append((method, params))
        assert method == "createfolderifnotexists"
        return {"metadata": {"folderid": 99}}

    svc._call = fake_call

    result = await svc.ensure_path("NewFolder")
    assert result == 99
    assert len(calls) == 1


async def test_ensure_path_exact_match_preferred_over_twin(monkeypatch):
    svc = PCloudService()
    # Twin listed first — see test_lookup_path_exact_match_preferred.
    svc.list_files = _fake_list_files({
        "0": [_folder(2, "abc"), _folder(1, "ABC")],
    })

    async def fake_call(method, params=None):
        raise AssertionError(f"unexpected pCloud call: {method}({params})")

    svc._call = fake_call

    result = await svc.ensure_path("ABC")
    assert result == 1
