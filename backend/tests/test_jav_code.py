import pytest

from app.services.jav_code import (
    clean_listing_name,
    ext_of,
    extract_jav_code,
    extract_jav_code_full,
    is_video,
    normalize_code,
    safe_folder_name,
)


@pytest.mark.parametrize(
    ("name", "want"),
    [
        # Plain forms
        ("DAM-043", "DAM-043"),
        ("dam-043", "DAM-043"),
        ("ABP-123.mp4", "ABP-123"),
        # Site-prefix garbage from BT names
        ("kfa55.com@483DAM-043", "DAM-043"),
        ("第一會所新片@SIS001@ABP-543", "ABP-543"),
        # Squished form gets its hyphen back
        ("SACE022MP4", "SACE-022"),
        # Numeric prefixes are always stripped (JavBus catalogs the bare code)
        ("259LUXU-1543", "LUXU-1543"),
        ("300MIUM-1090", "MIUM-1090"),
        ("200GANA-2156.mkv", "GANA-2156"),
        # Chinese-sub suffix and variant letter are dropped
        ("SNOS-015ch.mp4", "SNOS-015"),
        ("SDMM-14903C", "SDMM-14903"),
        ("ABP-123A", "ABP-123"),
        # No code at all
        ("readme.txt", None),
        ("", None),
    ],
)
def test_extract_jav_code(name, want):
    assert extract_jav_code(name) == want


@pytest.mark.parametrize(
    ("name", "want"),
    [
        # Variant letter is KEPT in the full form
        ("SDMM-14903C", "SDMM-14903C"),
        ("ABP-123A.mp4", "ABP-123A"),
        # But the Chinese-sub marker is not a variant
        ("SNOS-015ch.mp4", "SNOS-015"),
        # Numeric prefix still stripped
        ("259LUXU-1543", "LUXU-1543"),
        ("DAM-043", "DAM-043"),
        ("", None),
    ],
)
def test_extract_jav_code_full(name, want):
    assert extract_jav_code_full(name) == want


def test_normalize_code_collapses_variants():
    assert normalize_code("483DAM-043") == "DAM-043"
    assert normalize_code("dam043") == "DAM-043"
    assert normalize_code("not a code") == ""


@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("回胴錄 - 系列 - 影片", "回胴錄"),
        ("葵つかさ - 女優 - 影片", "葵つかさ"),
        ("already clean", "already clean"),
        ("", ""),
    ],
)
def test_clean_listing_name(name, want):
    assert clean_listing_name(name) == want


def test_clean_listing_name_idempotent():
    once = clean_listing_name("回胴錄 - 系列 - 影片")
    assert clean_listing_name(once) == once


def test_safe_folder_name():
    assert safe_folder_name('a/b\\c:d*e?f"g<h>i|j') == "abcdefghij"
    assert safe_folder_name("   ") == ""
    assert safe_folder_name("///", fallback="fb") == "fb"
    assert len(safe_folder_name("x" * 200)) == 64


def test_ext_and_is_video():
    assert ext_of("movie.MP4") == ".mp4"
    assert ext_of("noext") == ""
    assert is_video("a.mkv") is True
    assert is_video("a.jpg") is False
