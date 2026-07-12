"""Presence walk tolerates both flat (<name>/<code>) and nested
(<studio>/<series>/<code>) PikPak layouts so play-by-code keeps working
during the 製作商 migration."""

from types import SimpleNamespace

import app.services.pikpak_presence as pp


def _node(name, id, folder=True):
    return SimpleNamespace(
        name=name, id=id, kind="drive#folder" if folder else "drive#file"
    )


def _make_index(tree):
    """tree: {dir_id: [nodes]}. Build an index whose _list reads it."""
    idx = pp.PikPakPresenceIndex()

    async def fake_list(parent_id):
        return list(tree.get(parent_id, []))

    idx._list = fake_list  # type: ignore[method-assign]
    return idx


async def test_flat_layout_depth1():
    # AVBT/系列/<name>/<code>
    tree = {
        "kindroot": [_node("回胴録", "nameA"), _node("風俗タワー", "nameB")],
        "nameA": [_node("MIDV-001", "c1"), _node("MIDV-002.mp4", "c2", folder=False)],
        "nameB": [_node("YRK-288", "c3")],
    }
    idx = _make_index(tree)
    codes = await idx._collect_kind("AVBT/系列", "kindroot")
    assert codes == {"MIDV-001", "MIDV-002", "YRK-288"}
    assert idx.paths_for("MIDV-001") == ["AVBT/系列/回胴録/MIDV-001"]
    assert idx.paths_for("YRK-288") == ["AVBT/系列/風俗タワー/YRK-288"]


async def test_nested_studio_series_depth2():
    # AVBT/製作商/<studio>/<series>/<code>
    tree = {
        "studioroot": [_node("プレステージ", "st75")],
        "st75": [_node("風俗タワー", "seU0g"), _node("天然成分由来", "seTs9")],
        "seU0g": [_node("YRK-288", "m1"), _node("ABF-292.mp4", "m2", folder=False)],
        "seTs9": [_node("ABW-100", "m3")],
    }
    idx = _make_index(tree)
    codes = await idx._collect_kind("AVBT/製作商", "studioroot")
    assert codes == {"YRK-288", "ABF-292", "ABW-100"}
    assert idx.paths_for("YRK-288") == ["AVBT/製作商/プレステージ/風俗タワー/YRK-288"]
    assert idx.paths_for("ABW-100") == ["AVBT/製作商/プレステージ/天然成分由来/ABW-100"]


async def test_mixed_old_and_new_under_same_root():
    # During migration: some files still flat, some already nested.
    tree = {
        "root": [_node("旧作", "oldname"), _node("プレステージ", "st75")],
        "oldname": [_node("OLD-001", "o1")],  # depth-1 code leaf
        "st75": [_node("風俗タワー", "seU0g")],
        "seU0g": [_node("NEW-001", "n1")],  # depth-2 code leaf
    }
    idx = _make_index(tree)
    codes = await idx._collect_kind("AVBT/製作商", "root")
    assert codes == {"OLD-001", "NEW-001"}


async def test_depth_cap_and_unrecognized():
    # A non-code folder deeper than the cap is not descended; a stray
    # non-code file is counted unrecognized.
    tree = {
        "root": [_node("a", "d1"), _node("junk.txt", "j1", folder=False)],
        "d1": [_node("b", "d2")],
        "d2": [_node("c", "d3")],
        "d3": [_node("d", "d4")],  # depth 4 — beyond _MAX_KIND_DEPTH(3)
        "d4": [_node("DEEP-001", "x1")],  # should NOT be reached
    }
    idx = _make_index(tree)
    codes = await idx._collect_kind("AVBT/製作商", "root")
    assert codes == set()
    assert not idx.paths_for("DEEP-001")
    # the stray file is recorded as unrecognized in diagnostics
    assert any(u["name"] == "junk.txt" for u in idx._unrecognized)
