"""Collision copies: a second magnet landing beside CODE.mp4 becomes
CODE(1).mp4, and nothing ever owned the leftover."""

from types import SimpleNamespace

import pytest

from app.services.dup_copies import collision_code, plan_group, sweep_dup_copies

GB = 1024 ** 3
MB = 1024 ** 2


def _f(name, size, fid="x", phase="PHASE_TYPE_COMPLETE", kind="drive#file"):
    return SimpleNamespace(name=name, size=size, id=fid, kind=kind, phase=phase)


@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("ABF-002.mp4", "ABF-002"),
        ("ABF-002(1).mp4", "ABF-002"),
        ("REBD-1013 (2).mp4", "REBD-1013"),
        ("SONE-092(1).mp4", "SONE-092"),
        # A real disc — the whole point of the (N)-only rule is to never
        # touch these. PikPak's suffix means two files claimed the SAME
        # name; a disc set claims different ones.
        ("SDMM-053_1.mp4", ""),
        ("SDMM-053_2.mp4", ""),
        # Still carrying BT noise / a quality tag: the rename plan's job.
        ("[88K.ME]MIDV-001.mp4", ""),
        ("SQTE-659_4KS.mp4", ""),
        ("MBRBA-121.H265.mp4", ""),
        # Not a video, no code.
        ("ABF-002.txt", ""),
        ("cover.jpg", ""),
    ],
)
def test_collision_code(name, want):
    assert collision_code(name) == want


def test_biggest_wins_and_the_rest_are_trashed():
    # SONE-092 live: the 26.86GB upgrade the backfill fetched, next to
    # the 8.16GB copy it was meant to replace.
    old = _f("SONE-092.mp4", 8 * GB, "old")
    new = _f("SONE-092(1).mp4", 26 * GB, "new")
    losers, rename = plan_group([old, new])
    assert [e.id for e in losers] == ["old"]
    assert rename is None          # rename waits for the trash to settle


def test_none_size_copy_defers_never_trashes():
    # PikPak can list a real file with size=None (#220/#225). Collapsing
    # it to 0 made it a guaranteed size-contest loser → the real upgrade
    # got trashed and the stale small copy renamed over it. A group with
    # any unknown size must defer: trash nothing this pass.
    known = _f("SONE-092.mp4", 8 * GB, "known")
    unknown = _f("SONE-092(1).mp4", None, "unknown")
    losers, rename = plan_group([known, unknown])
    assert losers == []
    assert rename is None


def test_identical_copies_still_lose_one():
    a = _f("ABF-010.mp4", 7 * GB, "a")
    b = _f("ABF-010(1).mp4", 7 * GB, "b")
    losers, _rename = plan_group([a, b])
    assert len(losers) == 1


def test_tie_is_broken_the_same_way_every_run():
    a = _f("ABF-010.mp4", 7 * GB, "a")
    b = _f("ABF-010(1).mp4", 7 * GB, "b")
    assert [e.id for e in plan_group([a, b])[0]] == \
           [e.id for e in plan_group([b, a])[0]]


def test_lone_survivor_gets_renamed():
    # The second cadence: the loser is gone, so CODE.mp4 is free.
    only = _f("ABF-002(1).mp4", 13 * GB, "w")
    losers, rename = plan_group([only])
    assert losers == [] and rename is only


def test_an_already_canonical_lone_file_is_left_alone():
    losers, rename = plan_group([_f("ABF-002.mp4", 7 * GB, "w")])
    assert losers == [] and rename is None


def test_an_in_flight_copy_never_loses_a_size_contest():
    # A half-landed file reads small, and trashing it kills the transfer
    # and loses the partial (#129).
    done = _f("ABF-002.mp4", 7 * GB, "done")
    landing = _f("ABF-002(1).mp4", 200 * MB, "landing", phase="PHASE_TYPE_RUNNING")
    losers, rename = plan_group([done, landing])
    assert losers == [] and rename is None


class FakeSvc:
    def __init__(self, tree):
        self.tree = tree
        self.trashed: list[str] = []
        self.renamed: list[tuple[str, str]] = []

    async def lookup_folder_id(self, path):
        return "studio-root" if path.endswith("製作商") else ""

    async def list_all_files(self, folder_id, cap=5000):
        return list(self.tree.get(folder_id, [])), False

    async def trash_files(self, ids):
        self.trashed.extend(ids)
        return {}

    async def rename_file(self, file_id, new_name):
        self.renamed.append((file_id, new_name))
        return {}


def _tree():
    return {
        "studio-root": [_f("ABF", None, "s1", kind="drive#folder")],
        "s1": [_f("未分類", None, "ser1", kind="drive#folder")],
        "ser1": [
            _f("ABF-002.mp4", 7 * GB, "lose"),
            _f("ABF-002(1).mp4", 13 * GB, "keep"),
            _f("SDMM-053_1.mp4", 2 * GB, "part1"),   # real discs, hands off
            _f("SDMM-053_2.mp4", 2 * GB, "part2"),
            _f("MIDV-999.mp4", 5 * GB, "solo"),
        ],
    }


async def test_sweep_trashes_the_loser_only():
    svc = FakeSvc(_tree())
    summary = await sweep_dup_copies(svc, dry_run=False)
    assert svc.trashed == ["lose"]
    assert svc.renamed == []      # not until the trash settles
    assert summary["trashed"] == 1 and summary["errors"] == 0


async def test_sweep_renames_on_the_next_pass():
    tree = _tree()
    tree["ser1"] = [e for e in tree["ser1"] if e.id != "lose"]
    svc = FakeSvc(tree)
    await sweep_dup_copies(svc, dry_run=False)
    assert svc.trashed == []
    assert svc.renamed == [("keep", "ABF-002.mp4")]


async def test_dry_run_touches_nothing():
    svc = FakeSvc(_tree())
    summary = await sweep_dup_copies(svc, dry_run=True)
    assert svc.trashed == [] and svc.renamed == []
    assert summary["trashed"] == 1 and summary["dry_run"] is True
