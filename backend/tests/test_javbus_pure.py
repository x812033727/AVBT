import pytest

from app.schemas import Magnet
from app.scrapers.javbus import extract_btih, parse_size, pick_best_magnet

BTIH = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"


def mk(
    name="x",
    btih=BTIH,
    size="1.00GB",
    date="2024-01-01",
    is_hd=False,
    has_subtitle=False,
):
    return Magnet(
        name=name,
        link=f"magnet:?xt=urn:btih:{btih}&dn={name}",
        size=size,
        date=date,
        is_hd=is_hd,
        has_subtitle=has_subtitle,
    )


def test_extract_btih():
    assert extract_btih(f"magnet:?xt=urn:btih:{BTIH.lower()}") == BTIH
    assert extract_btih("not a magnet") == ""
    assert extract_btih("") == ""


@pytest.mark.parametrize(
    ("s", "want"),
    [
        ("2.34GB", 2.34 * 1024**3),
        ("500MB", 500 * 1024**2),
        ("1.5GiB", 1.5 * 1024**3),
        ("123 KB", 123 * 1024),
        ("7B", 7.0),
        ("garbage", 0.0),
        ("", 0.0),
    ],
)
def test_parse_size(s, want):
    assert parse_size(s) == pytest.approx(want)


def test_pick_prefers_subtitle_then_hd_then_size():
    plain_big = mk("plain-big", "A" * 40, size="4.00GB")
    hd_small = mk("hd-small", "B" * 40, size="2.00GB", is_hd=True)
    hd_sub = mk("hd-sub", "C" * 40, size="1.00GB", is_hd=True, has_subtitle=True)
    best = pick_best_magnet([plain_big, hd_small, hd_sub], hd_only=False)
    assert best.name == "hd-sub"


def test_hd_only_is_soft():
    # hd_only prefers HD but falls back when nothing is HD.
    only_plain = mk("plain", "A" * 40)
    assert pick_best_magnet([only_plain], hd_only=True).name == "plain"
    hd = mk("hd", "B" * 40, is_hd=True)
    assert pick_best_magnet([only_plain, hd], hd_only=True).name == "hd"


def test_skip_hashes_filters_sent():
    a = mk("a", "A" * 40)
    b = mk("b", "B" * 40)
    best = pick_best_magnet([a, b], hd_only=False, skip_hashes={"A" * 40})
    assert best.name == "b"
    assert pick_best_magnet([a], hd_only=False, skip_hashes={"A" * 40}) is None


def test_size_window():
    small = mk("small", "A" * 40, size="500MB")
    big = mk("big", "B" * 40, size="8.00GB")
    unknown = mk("unknown", "C" * 40, size="")
    got = pick_best_magnet([small, big], hd_only=False, min_size_mb=1024)
    assert got.name == "big"
    got = pick_best_magnet([small, big], hd_only=False, max_size_mb=1024)
    assert got.name == "small"
    # Unknown sizes never get rejected by the window.
    got = pick_best_magnet([unknown], hd_only=False, min_size_mb=1024)
    assert got.name == "unknown"


def test_prefer_max_size_is_soft_cap():
    small = mk("small", "A" * 40, size="1.00GB")
    big = mk("big", "B" * 40, size="9.00GB")
    # Both fit? prefer the one under the soft cap even though big sorts first.
    got = pick_best_magnet([small, big], hd_only=False, prefer_max_size_mb=2048)
    assert got.name == "small"
    # Everything oversized → fall back instead of returning nothing.
    got = pick_best_magnet([big], hd_only=False, prefer_max_size_mb=2048)
    assert got.name == "big"


def test_empty_input():
    assert pick_best_magnet([], hd_only=False) is None
