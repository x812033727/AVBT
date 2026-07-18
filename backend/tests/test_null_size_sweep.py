"""Systematic sweep of the ``(size or 0)`` landmine (#220 lesson):
PikPak can list real files with size=None; collapsing None to 0 routes
them into junk/copy/outlier branches — two of which permanently delete.
Principle: size=None → assume legit → always the SAFE branch.
"""

from types import SimpleNamespace

from app.services.finalize import build_finalize_plan
from app.services.rename_plan import (
    _split_size_outliers,
    low_bitrate_copies,
)
from app.services.series_junk import is_series_junk


def _f(name, fid, size, duration=7200.0):
    return SimpleNamespace(
        id=fid, name=name, kind="drive#file", size=size, parent_id=None,
        created_time=None, thumbnail_link=None,
        phase="PHASE_TYPE_COMPLETE", duration=duration,
    )


# --- finalize.py:197 — None-size sibling must NEVER be ad_purged --------

def test_null_size_group_member_goes_to_trash_not_purge():
    big = _f("ABC-001.mp4", "a", 4_000_000_000)
    unknown = _f("ABC-001 (1).mp4", "b", None)
    plan = build_finalize_plan("ABC-001", [(big, "root"), (unknown, "root")], "root")
    purged = {f.id for f in plan.purge_files}
    trashed = {f.id for f in plan.trash_files}
    assert "b" not in purged          # permanent delete forbidden on None
    assert "b" in trashed or "b" in {k.id for k, _ in plan.keep}


# --- finalize.py:163 — None member must not break a part set ------------

def test_null_size_member_keeps_part_set_together():
    m1 = _f("ABC-002_1.mp4", "p1", 2_000_000_000)
    m2 = _f("ABC-002_2.mp4", "p2", None)
    plan = build_finalize_plan(
        "ABC-002", [(m1, "root"), (m2, "root")], "root"
    )
    kept = {k.id for k, _ in plan.keep}
    assert kept == {"p1", "p2"}       # both discs kept as parts


# --- rename_plan outliers: None never an outlier ------------------------

def test_null_size_never_outlier():
    files = [
        _f("X_1.mp4", "a", 3_000_000_000),
        _f("X_2.mp4", "b", 3_000_000_000),
        _f("X (copy).mp4", "c", None),
    ]
    parts, outliers = _split_size_outliers(files, "X")
    assert all(f.id != "c" for f in outliers)


# --- rename_plan low_bitrate_copies: None never a copy ------------------

def test_null_size_never_low_bitrate_copy():
    big = _f("Y.mp4", "a", 5_000_000_000)
    unknown = _f("Y-SD.mp4", "b", None)
    assert all(f.id != "b" for f in low_bitrate_copies([big, unknown]))


# --- series_junk: None-size video/container never junk ------------------

def test_series_junk_null_video_not_junk():
    assert is_series_junk("ABC-003.mp4", None, "PHASE_TYPE_COMPLETE", video_bytes=0) is False


def test_series_junk_null_container_not_retired():
    # video exists, container size unknown → keep the container.
    assert is_series_junk("ABC-003.iso", None, "PHASE_TYPE_COMPLETE",
                          video_bytes=5_000_000_000) is False


def test_series_junk_known_small_video_still_junk():
    assert is_series_junk("ad.mp4", 50_000_000, "PHASE_TYPE_COMPLETE", video_bytes=0) is True
