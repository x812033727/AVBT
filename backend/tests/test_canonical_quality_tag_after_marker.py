"""A quality tag *after* the part marker (``IPVR00301_1_HQ.mp4``) must not
hide the marker from the canonical.

Live loss 2026-08-31 17:09 UTC: ``IPVR00301_1_HQ.mp4`` (3.08GB) and
``IPVR00301_2_HQ.mp4`` (3.10GB) had sat side by side in the 系列 folder
since 08-05. ``_canonical_video_name`` returned two different canonicals
(``IPVR00301_1_HQ`` / ``IPVR00301_2_HQ``), so phase-2 never saw a part
set, fell through to the same-code dedupe and trashed disc 1 as a
"duplicate" of disc 2 — then renamed disc 2 to ``IPVR-301.mp4``.
``HNVR00135_1_HQ`` / ``_2_HQ`` sit in the library with the same shape.
"""
from types import SimpleNamespace as NS

import pytest

from app.services.jav_code import is_video
from app.services.rename_plan import (
    _build_video_rename_plan,
    _canonical_video_name,
)

GB = 1024 ** 3
PART_MIN = 500 * 1024 * 1024


@pytest.mark.parametrize(
    ("name", "canon"),
    [
        ("IPVR00301_1_HQ.mp4", "IPVR-301"),
        ("IPVR00301_2_HQ.mp4", "IPVR-301"),
        ("HNVR00135_2_HQ.mp4", "HNVR-135"),
        ("IPVR-301_1_HQ.mp4", "IPVR-301"),
        ("IPVR00301-1_HQ.mp4", "IPVR-301"),
        ("ABC-123_2-4K.mp4", "ABC-123"),
        ("ABC-123_1_HQ.mp4", "ABC-123"),
    ],
)
def test_quality_tag_after_part_marker_keeps_code_canonical(name, canon):
    assert _canonical_video_name(name) == canon


@pytest.mark.parametrize(
    ("name", "canon"),
    [
        # Marker-less quality tags already collapse to the code today
        # (SD/HD/4K/1080P); HQ now gets the same treatment.
        ("ABC-123_1080P.mp4", "ABC-123"),
        ("ABC-123-SD.mp4", "ABC-123"),
        ("ABC-123_HQ.mp4", "ABC-123"),
        # A tag glued to more text is not a tag — untouched.
        ("KAVR-227A-4K60FPS.mp4", "KAVR-227A-4K60FPS"),
    ],
)
def test_marker_less_tags_follow_existing_treatment(name, canon):
    assert _canonical_video_name(name) == canon


def _f(i, name, size):
    return NS(id=f"id{i}", name=name, size=size, kind="drive#file")


def test_phase2_reads_hq_tagged_zero_padded_parts_as_one_set():
    files = [
        _f(1, "IPVR00301_1_HQ.mp4", int(3.08 * GB)),
        _f(2, "IPVR00301_2_HQ.mp4", int(3.10 * GB)),
    ]
    plan, members = _build_video_rename_plan(
        files, PART_MIN, is_video, require_marker=True
    )
    assert members == {"IPVR00301_1_HQ.mp4", "IPVR00301_2_HQ.mp4"}
    assert plan == {
        "IPVR00301_1_HQ.mp4": "IPVR-301_1.mp4",
        "IPVR00301_2_HQ.mp4": "IPVR-301_2.mp4",
    }


def test_hq_parts_beside_normalised_parts_keep_their_slots():
    # HNVR-135 live shape: the normalised pair already holds _1/_2, the
    # HQ-tagged pair must join the set (not be deduped) and take _3/_4
    # only when sizes differ — same-size twins are withheld by the
    # near-identical gate, which is existing behaviour.
    files = [
        _f(1, "HNVR-135_1.mp4", 4 * GB),
        _f(2, "HNVR-135_2.mp4", 4 * GB + 5 * 1024 * 1024),
        _f(3, "HNVR00135_1_HQ.mp4", 7 * GB),
        _f(4, "HNVR00135_2_HQ.mp4", 7 * GB + 5 * 1024 * 1024),
    ]
    plan, members = _build_video_rename_plan(
        files, PART_MIN, is_video, require_marker=True
    )
    assert members == {f.name for f in files}
    assert "HNVR00135_1_HQ.mp4" in plan and "HNVR00135_2_HQ.mp4" in plan
    assert not any(t in ("HNVR-135_1.mp4", "HNVR-135_2.mp4") for t in plan.values())
