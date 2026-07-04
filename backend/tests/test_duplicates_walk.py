from types import SimpleNamespace

import pytest

from app.services.duplicates import _walk_codes


def item(id_, name, kind="file"):
    return SimpleNamespace(id=id_, name=name, kind=kind)


def tree_lister(tree: dict[str, list]):
    async def list_fn(parent_id: str) -> list:
        return tree.get(parent_id, [])

    return list_fn


async def run_walk(list_fn, *, max_depth=8, cap=20000):
    result = None
    async for ev in _walk_codes(
        "root", list_fn, ("folder",), max_depth=max_depth, cap=cap
    ):
        if ev["kind"] == "result":
            result = ev
    return result


async def test_codes_collected_with_paths_and_ids():
    tree = {
        "root": [item("d1", "女優", kind="folder"), item("f1", "ABP-123.mp4")],
        "d1": [item("f2", "kfa55.com@483DAM-043.mkv")],
    }
    res = await run_walk(tree_lister(tree))
    assert res["codes"] == {
        "ABP-123": [{"path": "ABP-123.mp4", "id": "f1", "is_folder": False}],
        "DAM-043": [
            {
                "path": "女優/kfa55.com@483DAM-043.mkv",
                "id": "f2",
                "is_folder": False,
            }
        ],
    }
    assert res["partial"] is False
    assert res["items_seen"] == 3


async def test_code_folders_are_flagged():
    tree = {
        "root": [item("d1", "DAM-043", kind="folder")],
        "d1": [item("f1", "DAM-043.mp4")],
    }
    res = await run_walk(tree_lister(tree))
    hits = res["codes"]["DAM-043"]
    assert {h["id"]: h["is_folder"] for h in hits} == {"d1": True, "f1": False}


async def test_same_code_two_paths():
    tree = {
        "root": [item("a", "ABP-123.mp4"), item("b", "ABP-123ch.mp4")],
    }
    res = await run_walk(tree_lister(tree))
    assert sorted(h["path"] for h in res["codes"]["ABP-123"]) == [
        "ABP-123.mp4",
        "ABP-123ch.mp4",
    ]


async def test_depth_cap_stops_recursion():
    tree = {
        "root": [item("d1", "level1", kind="folder")],
        "d1": [item("d2", "level2", kind="folder")],
        "d2": [item("f", "ABP-999.mp4")],
    }
    res = await run_walk(tree_lister(tree), max_depth=1)
    # d2 sits at depth 2 → never listed, its file never seen.
    assert res["codes"] == {}


async def test_item_cap_marks_partial():
    tree = {"root": [item(f"f{i}", f"ABP-{i:03d}.mp4") for i in range(50)]}
    res = await run_walk(tree_lister(tree), cap=10)
    assert res["partial"] is True
    assert res["items_seen"] == 10


async def test_root_failure_raises():
    async def list_fn(parent_id: str) -> list:
        raise RuntimeError("logged out")

    with pytest.raises(RuntimeError, match="logged out"):
        await run_walk(list_fn)


async def test_child_failure_is_skipped():
    async def list_fn(parent_id: str) -> list:
        if parent_id == "root":
            return [item("bad", "壞資料夾", kind="folder"), item("f", "ABP-111.mp4")]
        raise RuntimeError("unreadable subfolder")

    res = await run_walk(list_fn)
    assert [h["path"] for h in res["codes"]["ABP-111"]] == ["ABP-111.mp4"]
    assert res["partial"] is False
