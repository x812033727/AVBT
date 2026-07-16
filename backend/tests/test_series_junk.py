"""Loose junk in a 系列 folder: finalize only ever purged inside a 番號
folder, so the flatten layout let ad clips pile up unowned."""

from types import SimpleNamespace

import pytest

from app.services.series_junk import is_series_junk, purge_series_junk

GB = 1024 ** 3
MB = 1024 ** 2


def _f(name, size, kind="drive#file", fid="x", phase="PHASE_TYPE_COMPLETE"):
    return SimpleNamespace(name=name, size=size, kind=kind, id=fid, phase=phase)


@pytest.mark.parametrize(
    ("name", "size", "want"),
    [
        ("社 区 最 新 情 报.mp4", 17 * MB, True),      # ad clip
        ("ZB-555.mp4", 5 * MB, True),                  # spam wearing a code
        ("MFC-261.mp4", 3 * GB, False),                # the real work
        ("MFC-261_2.mp4", 800 * MB, False),            # a real disc
        ("18p2p帳號.rtf", 1 * MB, True),                # non-video junk
        ("cover.jpg", 200, True),
        ("SNIS-494.iso", 23 * GB, False),              # rescued disc image
        ("AP-619.zip", 2 * GB, False),                 # archived work
    ],
)
def test_is_series_junk(name, size, want):
    assert is_series_junk(name, size) is want


def test_in_flight_file_is_never_junk():
    # A file PikPak is still writing reads tiny — trashing it kills the
    # transfer (#129). Size must not decide while phase says RUNNING.
    assert is_series_junk("MFC-261.mp4", 5 * MB, "PHASE_TYPE_RUNNING") is False


def test_container_becomes_junk_once_a_credible_video_lands():
    # The tail of the swap: the disc image is kept while it is the sole
    # copy of the work, and retires — trashed, not purged — once a real
    # video for the code exists. An .iso holds the film uncompressed, so
    # a same-quality mp4 is legitimately a fraction of its size.
    assert is_series_junk("EKDV-434.iso", 4 * GB) is False
    assert is_series_junk("EKDV-434.iso", 4 * GB, video_bytes=1400 * MB) is True


def test_container_survives_a_downgrade_replacement():
    # SNIS-494: a 23.85GB Blu-ray image against the 2.0GB avi the swap
    # found — 8%. That is not a re-encode, it is a worse rip, and the only
    # high-quality copy must not be retired for it.
    assert is_series_junk("SNIS-494.iso", 23 * GB, video_bytes=2 * GB) is False


class FakeSvc:
    def __init__(self, tree):
        self.tree = tree
        self.trashed: list[str] = []

    async def lookup_folder_id(self, path):
        return "studio-root" if path.endswith("製作商") else ""

    async def list_all_files(self, folder_id, cap=5000):
        return list(self.tree.get(folder_id, [])), False

    async def trash_files(self, ids):
        self.trashed.extend(ids)
        return {}


def _tree():
    return {
        "studio-root": [_f("MOONFORCE", None, "drive#folder", "studio1")],
        "studio1": [_f("未分類", None, "drive#folder", "series1")],
        "series1": [
            _f("MFC-260.mp4", 3 * GB, fid="keep1"),
            _f("社 区 最 新 情 报.mp4", 17 * MB, fid="junk1"),
            _f("ZB-555.mp4", 5 * MB, fid="junk2"),
            _f("SNIS-494.iso", 5 * GB, fid="keep2"),
            _f("MFC-261.mp4", 5 * MB, fid="inflight",
               phase="PHASE_TYPE_RUNNING"),
            _f("aavv38.xyz@435MFC-261", None, "drive#folder", "wrapper"),
        ],
    }


async def test_purge_trashes_only_junk():
    svc = FakeSvc(_tree())
    summary = await purge_series_junk(svc, dry_run=False)
    assert sorted(svc.trashed) == ["junk1", "junk2"]
    assert summary == {"scanned": 5, "trashed": 2, "dry_run": False}


async def test_walk_trashes_a_superseded_container_only():
    # SNIS-494.iso survives while it is alone (the _tree case above); add
    # the swapped-in video and the same walk retires it. The still-alone
    # AP-619.zip must not be swept along with it.
    tree = _tree()
    tree["series1"] += [
        _f("SNIS-494.mp4", 4 * GB, fid="swapped"),
        _f("AP-619.zip", 2 * GB, fid="lonely"),
    ]
    svc = FakeSvc(tree)
    await purge_series_junk(svc, dry_run=False)
    assert sorted(svc.trashed) == ["junk1", "junk2", "keep2"]


async def test_container_retires_when_its_video_lands_in_a_drifted_folder():
    # Series folder names drift, so the swapped-in video does not reliably
    # land beside the container it replaces: live, SNIS-494.iso sat in
    # "新人NO.1 STYLE" while its .avi arrived in "新人NO.1STYLE". A
    # per-folder answer keeps every drifted container forever.
    tree = _tree()
    tree["studio1"].append(_f("別的系列", None, "drive#folder", "series2"))
    tree["series2"] = [_f("SNIS-494.avi", 4 * GB, fid="swapped")]
    svc = FakeSvc(tree)
    await purge_series_junk(svc, dry_run=False)
    assert "keep2" in svc.trashed          # the .iso, one folder over
    assert "swapped" not in svc.trashed


async def test_half_landed_video_does_not_condemn_its_container():
    # An in-flight replacement is not proof of anything: if it dies the
    # container is all that is left. Only a COMPLETE video retires one.
    tree = _tree()
    tree["series1"].append(
        _f("SNIS-494.mp4", 4 * GB, fid="partial", phase="PHASE_TYPE_RUNNING")
    )
    svc = FakeSvc(tree)
    await purge_series_junk(svc, dry_run=False)
    assert "keep2" not in svc.trashed


async def test_dry_run_touches_nothing():
    svc = FakeSvc(_tree())
    summary = await purge_series_junk(svc, dry_run=True)
    assert svc.trashed == []
    assert summary["trashed"] == 2 and summary["dry_run"] is True
