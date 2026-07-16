"""Folder names drift, so resolving one must not fork a second folder.

JavBus returns the same series as "新人NO.1 STYLE" one day and
"新人NO.1STYLE" the next. path_to_id matches exactly, so the drifted
spelling used to create a twin and split the series across two folders —
live 2026-07-16: 6 such pairs, still being created by new downloads.
"""

from types import SimpleNamespace

import pytest

from app.services.jav_code import folder_key
from app.services.pikpak import PikPakService


@pytest.mark.parametrize(
    ("a", "b"),
    [  # every drift pair found in the live library
        ("新人NO.1 STYLE", "新人NO.1STYLE"),
        ("ALL NUDE", "ALLNUDE"),
        ("INSTANT LOVE", "INSTANTLOVE"),
        ("First Impression", "FirstImpression"),
        ("もっと、汁 120%", "もっと、汁120%"),
        ("新人 プレステージ専属デビュー", "新人プレステージ専属デビュー"),
    ],
)
def test_drifted_names_share_a_key(a, b):
    assert folder_key(a) == folder_key(b)


def test_different_series_keep_different_keys():
    assert folder_key("ABC-1") != folder_key("ABD-1")
    assert folder_key("First Impression") != folder_key("First Impressions")


class FakeSvc(PikPakService):
    """Only the two primitives _canonical_path leans on."""

    def __init__(self, tree, path_ids=None):
        super().__init__()
        self._tree = tree          # parent_id -> [(name, id)]
        self._path_ids = path_ids or {}
        self.created: list[str] = []

    async def list_all_files(self, parent_id="", *, cap=5000):
        return [SimpleNamespace(name=n, id=i, kind="drive#folder")
                for n, i in self._tree.get(parent_id, [])], False

    async def _call(self, fn):
        return []      # path_to_id finds nothing by exact name

    async def lookup_folder_id(self, name):
        return self._path_ids.get(name, "")


def _tree():
    return {
        "": [("AVBT", "avbt")],
        "avbt": [("製作商", "studio")],
        "studio": [("エスワンナンバーワンスタイル", "s1")],
        "s1": [("新人NO.1 STYLE", "series-real")],
    }


async def test_drifted_leaf_resolves_to_the_existing_folder():
    svc = FakeSvc(_tree())
    got = await svc._canonical_path(
        "AVBT/製作商/エスワンナンバーワンスタイル/新人NO.1STYLE")
    assert got == "AVBT/製作商/エスワンナンバーワンスタイル/新人NO.1 STYLE"


async def test_a_genuinely_new_folder_is_left_alone():
    # Nothing to reuse — the caller's name must survive untouched, or a
    # brand-new series could never be created.
    svc = FakeSvc(_tree())
    path = "AVBT/製作商/エスワンナンバーワンスタイル/まったく新しい系列"
    assert await svc._canonical_path(path) == path


async def test_segments_below_a_missing_one_are_untouched():
    svc = FakeSvc(_tree())
    path = "AVBT/製作商/知らないメーカー/新人NO.1STYLE"
    assert await svc._canonical_path(path) == path


async def test_lookup_falls_back_to_the_twin():
    # Reads must agree with writes: once a drifted name reuses an existing
    # folder, an exact-only lookup would miss the folder its files are in
    # (live: presence reported MAS-096 gone for exactly this reason).
    svc = FakeSvc(_tree(), path_ids={
        "AVBT/製作商/エスワンナンバーワンスタイル/新人NO.1 STYLE": "series-real"})
    got = await PikPakService.lookup_folder_id(
        svc, "AVBT/製作商/エスワンナンバーワンスタイル/新人NO.1STYLE")
    assert got == "series-real"


async def test_resolution_is_memoised():
    # This runs on every lookup miss, and misses are routine — an
    # unmemoised walk would re-list the drive root per code.
    svc = FakeSvc(_tree())
    calls = []
    orig = svc.list_all_files

    async def counting(parent_id="", *, cap=5000):
        calls.append(parent_id)
        return await orig(parent_id, cap=cap)

    svc.list_all_files = counting
    path = "AVBT/製作商/エスワンナンバーワンスタイル/新人NO.1STYLE"
    await svc._canonical_path(path)
    n = len(calls)
    await svc._canonical_path(path)
    assert len(calls) == n
