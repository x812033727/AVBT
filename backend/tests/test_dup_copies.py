"""Collision copies: a second magnet landing beside CODE.mp4 becomes
CODE(1).mp4, and nothing ever owned the leftover."""

from types import SimpleNamespace

import pytest

from app.services.dup_copies import (
    collision_code,
    part_copy_original,
    plan_group,
    plan_part_copies,
    sweep_dup_copies,
)

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


# --- ``CODE_N (K)``: a disc of a multi-part set that landed twice -------

@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("KAVR-527_1 (2).mp4", "KAVR-527_1.mp4"),
        ("PRVR-089_3 (3).mp4", "PRVR-089_3.mp4"),
        ("MDVR-451_2(2).mp4", "MDVR-451_2.mp4"),
        # The originals themselves, and the bare-code copies plan_group owns.
        ("KAVR-527_1.mp4", ""),
        ("ABF-002(1).mp4", ""),
        # BT noise / quality tags: the rename plan's job, never ours.
        ("4k2.me@kavr00527_2_8k (2).mp4", ""),
        ("hhd800.com@KAVR-527_1 (2).mp4", ""),
        ("KAVR-527_1_8K (2).mp4", ""),
        ("KAVR-527_1 (2).jpg", ""),
    ],
)
def test_part_copy_original(name, want):
    assert part_copy_original(name) == want


def test_byte_identical_part_copy_loses_and_the_original_stays():
    # MDVR-451 live 2026-11-23: PikPak finished a 4-day-old task for a
    # set that had already landed — every disc twice, same bytes.
    files = [
        _f("MDVR-451_1.mp4", 4_630_000_001, "o1"),
        _f("MDVR-451_1 (2).mp4", 4_630_000_001, "c1"),
        _f("MDVR-451_2.mp4", 3_430_000_002, "o2"),
        _f("MDVR-451_2 (2).mp4", 3_430_000_002, "c2"),
        _f("MDVR-451_2 (3).mp4", 3_430_000_002, "c3"),
    ]
    assert [e.id for e in plan_part_copies(files)] == ["c1", "c2", "c3"]


def test_part_copy_of_a_different_size_is_left_for_a_human():
    # Another release's disc 1, cut differently — not provably a copy.
    files = [
        _f("IPVR-371_1.mp4", 5 * GB, "o"),
        _f("IPVR-371_1 (2).mp4", 9 * GB, "bigger"),
        _f("IPVR-371_2.mp4", 9 * GB, "o2"),
        _f("IPVR-371_2 (2).mp4", 9 * GB - 1, "off-by-one"),
    ]
    assert plan_part_copies(files) == []


def test_part_copy_without_its_original_is_never_trashed():
    # The last copy standing must survive, whatever its name.
    assert plan_part_copies([_f("KAVR-527_1 (2).mp4", 6 * GB, "only")]) == []
    # Two suffixed copies and no original: neither proves the other.
    assert plan_part_copies([
        _f("KAVR-527_1 (2).mp4", 6 * GB, "a"),
        _f("KAVR-527_1 (3).mp4", 6 * GB, "b"),
    ]) == []


def test_part_copy_unknown_size_or_in_flight_defers():
    o = _f("KAVR-527_1.mp4", 6 * GB, "o")
    assert plan_part_copies([o, _f("KAVR-527_1 (2).mp4", None, "c")]) == []
    assert plan_part_copies([
        o, _f("KAVR-527_1 (2).mp4", 6 * GB, "c", phase="PHASE_TYPE_RUNNING"),
    ]) == []
    assert plan_part_copies([
        _f("KAVR-527_1.mp4", 6 * GB, "o", phase="PHASE_TYPE_RUNNING"),
        _f("KAVR-527_1 (2).mp4", 6 * GB, "c"),
    ]) == []
    assert plan_part_copies([
        _f("KAVR-527_1.mp4", 0, "o"), _f("KAVR-527_1 (2).mp4", 0, "c"),
    ]) == []


async def test_sweep_trashes_identical_part_copies_only():
    tree = _tree()
    tree["ser1"] += [
        _f("SDMM-053_1 (2).mp4", 2 * GB, "twin"),       # same bytes as part1
        _f("SDMM-053_2 (2).mp4", 3 * GB, "other-cut"),  # differs: hands off
    ]
    svc = FakeSvc(tree)
    summary = await sweep_dup_copies(svc, dry_run=False)
    assert sorted(svc.trashed) == ["lose", "twin"]
    assert svc.renamed == []
    assert summary["trashed"] == 2 and summary["errors"] == 0
