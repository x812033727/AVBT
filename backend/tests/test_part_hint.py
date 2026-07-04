import pytest

from app.services.jav_code import detect_part_hint


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("ABC-123-CD1.mp4", "ABC-123"),
        ("ABC-123 CD2", "ABC-123"),
        ("ABC-123-DISC 2", "ABC-123"),
        ("ABC-123.part2.mkv", "ABC-123"),
        ("ABC-123 pt-1", "ABC-123"),
        ("ABC-123 vol.3", "ABC-123"),
        ("某某企劃 上集", None),
        ("某某企劃 下巻", None),
        ("ABC-123-2.mp4", "ABC-123"),
        ("ABC-123_1.mp4", "ABC-123"),
        ("ABC-123B", "ABC-123"),
        ("hhd800.com@ABC-123A", None),  # code derived internally
        ("ABC-123CD2", "ABC-123"),
    ],
)
def test_part_hint_positive(name, code):
    assert detect_part_hint(name, code) != "", name


@pytest.mark.parametrize(
    ("name", "code"),
    [
        # Chinese-subtitle markers must never read as parts.
        ("ABC-123-C.mp4", "ABC-123"),
        ("ABC-123ch.mp4", "ABC-123"),
        ("ABC-123-CH.mp4", "ABC-123"),
        ("第一會所@ABC-123-C", None),
        # Resolution / date suffixes.
        ("ABC-123-4K.mp4", "ABC-123"),
        ("ABC-123-1080p.mp4", "ABC-123"),
        ("ABC-123 2024-01-02", "ABC-123"),
        # Plain single-file names.
        ("ABC-123.mp4", "ABC-123"),
        ("ABC-123", None),
        ("300MIUM-1090.mp4", None),
        ("kfa55.com@483DAM-043.mkv", None),
        ("", None),
    ],
)
def test_part_hint_negative(name, code):
    assert detect_part_hint(name, code) == "", name


def test_part_hint_returns_marker_text():
    assert detect_part_hint("ABC-123-CD2.mp4", "ABC-123") == "CD2"
    assert detect_part_hint("ABC-123-2.mp4", "ABC-123") == "-2"
    assert detect_part_hint("ABC-123B", "ABC-123") == "B"
    assert detect_part_hint("激情 上集", None) == "上集"
