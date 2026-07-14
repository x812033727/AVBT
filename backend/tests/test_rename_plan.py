"""Canonical-name / part-marker blind spots that defeated multi-disc
grouping (live loss 2026-07-14: HUNTA-578 disc B trashed as a
"duplicate" of disc A because the hyphen-less, ext-glued spelling
``HUNTA578AMP4`` never anchored to the code)."""

from types import SimpleNamespace

from app.services.rename_plan import (
    _build_video_rename_plan,
    _canonical_video_name,
    _part_marker_index,
)

GB = 1024 ** 3

TITLE_398 = (
    "ナースさんだらけのシェアハウスに入居したら男はボク1人で、"
    "しかもみんな普段からTバックだった！"
)


def _f(name: str, size: int = 2 * GB):
    return SimpleNamespace(name=name, size=size, kind="drive#file")


def _is_video(name: str) -> bool:
    return name.lower().endswith((".mp4", ".mkv", ".avi", ".wmv"))


def test_canonical_hyphenless_glued_ext():
    assert _canonical_video_name("HUNTA578AMP4.mp4") == "HUNTA-578"
    assert _canonical_video_name("HUNTA578Bmp4.mp4") == "HUNTA-578"
    assert _canonical_video_name("AP658A.mp4") == "AP-658"
    assert _canonical_video_name("AP658B.mp4") == "AP-658"
    assert _canonical_video_name("SKMJ044.mp4") == "SKMJ-044"


def test_canonical_head_paren_code_tag():
    assert (
        _canonical_video_name(f"(Hunter)(HUNTA-398){TITLE_398}_1.mp4")
        == "HUNTA-398"
    )
    assert (
        _canonical_video_name(f"(HUNTER)(HUNTA-398){TITLE_398}_2.mp4")
        == "HUNTA-398"
    )


def test_canonical_mid_name_code_still_untouched():
    # A free-text mid-name code must NOT collapse to the bare code —
    # a making-of would group with the main film and lose the dedup.
    assert _canonical_video_name("MIDV-001 making-of.mp4") != "MIDV-001"
    # x264-style tails keep their distinct canonical too.
    assert (_canonical_video_name("ABC-123 x264.mp4")
            != _canonical_video_name("ABC-123.mp4"))


def test_part_marker_hyphenless_letter():
    assert _part_marker_index("HUNTA578AMP4.mp4", "HUNTA-578") == 1
    assert _part_marker_index("HUNTA578Bmp4.mp4", "HUNTA-578") == 2
    assert _part_marker_index("AP658B.mp4", "AP-658") == 2


def test_part_marker_trailing_index_fallback():
    name = f"(Hunter)(HUNTA-398){TITLE_398}_2.mp4"
    assert _part_marker_index(name, "HUNTA-398") == 2
    # A trailing year must not claim a part slot.
    assert _part_marker_index("ABC-123 title_2024.mp4", "ABC-123") == 0
    # PikPak dup suffix ``(N)`` is not a part marker (ordering for those
    # comes from _dup_sort_index).
    assert _part_marker_index("HUNTA-513 (2).mp4", "HUNTA-513") == 0


def test_plan_groups_hyphenless_discs_as_parts():
    children = [
        _f("HUNTA578AMP4.mp4", int(2.26 * GB)),
        _f("HUNTA578Bmp4.mp4", int(2.09 * GB)),
    ]
    plan, members = _build_video_rename_plan(children, 500 * 1024 * 1024,
                                             _is_video)
    assert plan == {
        "HUNTA578AMP4.mp4": "HUNTA-578_1.mp4",
        "HUNTA578Bmp4.mp4": "HUNTA-578_2.mp4",
    }
    assert members == {"HUNTA578AMP4.mp4", "HUNTA578Bmp4.mp4"}


def test_plan_bare_old_file_cannot_shift_disc_slots():
    # An old whole-film rip the same size as the discs slips past the
    # size-outlier split; it must take the LEFTOVER slot, not _1.
    children = [
        _f("HUNTA-578.mp4", int(2.26 * GB)),
        _f("HUNTA578AMP4.mp4", int(2.26 * GB)),
        _f("HUNTA578Bmp4.mp4", int(2.09 * GB)),
    ]
    plan, _members = _build_video_rename_plan(children, 500 * 1024 * 1024,
                                              _is_video)
    assert plan["HUNTA578AMP4.mp4"] == "HUNTA-578_1.mp4"
    assert plan["HUNTA578Bmp4.mp4"] == "HUNTA-578_2.mp4"
    assert plan["HUNTA-578.mp4"] == "HUNTA-578_3.mp4"


def test_plan_head_paren_pair_with_old_outlier():
    # Real layout after the HUNTA-398 backfill: two ~5GB discs named by
    # title + a 2GB old rip. The old rip is a size outlier — it keeps
    # its name and never joins the part set.
    children = [
        _f(f"(Hunter)(HUNTA-398){TITLE_398}_1.mp4", int(5.89 * GB)),
        _f(f"(HUNTER)(HUNTA-398){TITLE_398}_2.mp4", int(5.36 * GB)),
        _f("HUNTA-398.mp4", 2 * GB),
    ]
    plan, members = _build_video_rename_plan(children, 500 * 1024 * 1024,
                                             _is_video)
    assert plan[f"(Hunter)(HUNTA-398){TITLE_398}_1.mp4"] == "HUNTA-398_1.mp4"
    assert plan[f"(HUNTER)(HUNTA-398){TITLE_398}_2.mp4"] == "HUNTA-398_2.mp4"
    assert "HUNTA-398.mp4" not in plan
    assert "HUNTA-398.mp4" not in members
