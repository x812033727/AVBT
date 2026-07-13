import pytest

from app.schemas import Magnet, MovieDetail
from app.services.part_estimate import estimate_multipart, parse_duration_minutes


def mk(size: str, part_hint: str = "", name: str = "X-1") -> Magnet:
    return Magnet(name=name, link="magnet:?xt=urn:btih:x", size=size, part_hint=part_hint)


def detail(duration: str = "", magnets=None) -> MovieDetail:
    return MovieDetail(code="X-1", title="t", duration=duration, magnets=magnets or [])


@pytest.mark.parametrize(
    ("s", "expected"),
    [
        ("471分鐘", 471),
        ("471 min", 471),
        ("170分鐘", 170),
        ("", None),
        ("分鐘", None),
        ("未知", None),
    ],
)
def test_parse_duration_minutes(s, expected):
    assert parse_duration_minutes(s) == expected


def test_ofje585_long_boxset_bare_names_is_multi():
    # 471-min compilation whose magnets are just the bare code (no marker).
    d = detail("471分鐘", [mk("20.10GB"), mk("19.81GB")])
    est = estimate_multipart(d)
    assert est.likely == "multi"
    assert est.duration_min == 471
    assert "片長" in est.reason


def test_ssis020_single_many_magnets_is_single():
    d = detail("170分鐘", [mk("6.00GB"), mk("5.20GB"), mk("2.07GB")])
    est = estimate_multipart(d)
    assert est.likely == "single"


def test_explicit_cd2_marker_wins_over_short_duration():
    d = detail("120分鐘", [mk("4.00GB", part_hint="CD2")])
    est = estimate_multipart(d)
    assert est.likely == "multi"
    assert est.part_markers == ["CD2"]


def test_empty_duration_is_unknown():
    d = detail("", [mk("18.00GB")])
    est = estimate_multipart(d)
    assert est.likely == "unknown"
    assert est.duration_min is None


def test_mid_band_large_file_is_multi():
    d = detail("200分鐘", [mk("18.00GB"), mk("6.00GB")])
    est = estimate_multipart(d)
    assert est.likely == "multi"
    assert est.max_size_gb == 18.0


def test_mid_band_normal_file_is_single():
    d = detail("200分鐘", [mk("6.00GB")])
    est = estimate_multipart(d)
    assert est.likely == "single"


@pytest.mark.parametrize(
    ("duration", "size", "expected"),
    [
        ("300分鐘", "6.00GB", "multi"),   # boundary hits rule 3
        ("299分鐘", "6.00GB", "single"),  # just under, small file
        ("179分鐘", "20.00GB", "single"),  # under long band, size alone never multi
    ],
)
def test_boundaries(duration, size, expected):
    assert estimate_multipart(detail(duration, [mk(size)])).likely == expected


def test_no_magnets_single_short():
    est = estimate_multipart(detail("90分鐘", []))
    assert est.likely == "single"
    assert est.max_size_gb is None
    assert est.part_markers == []


def test_dedupes_markers():
    d = detail("120分鐘", [mk("4GB", part_hint="CD1"), mk("4GB", part_hint="CD1"),
                          mk("4GB", part_hint="CD2")])
    est = estimate_multipart(d)
    assert est.likely == "multi"
    assert est.part_markers == ["CD1", "CD2"]
