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


# --- marker-noise triage (live sampling: "HMN-875J" style site tags) ---

def test_lone_letter_marker_is_noise_falls_to_duration():
    # HMN-875 real case: three magnets named "HMN-875J", 116-min single.
    d = detail("116分鐘", [mk("4.88GB"), mk("1.99GB", part_hint="J"),
                          mk("1.11GB", part_hint="J")])
    est = estimate_multipart(d)
    assert est.likely == "single"
    assert est.part_markers == ["J"]  # raw hints still surfaced


def test_two_distinct_variant_letters_are_significant():
    d = detail("120分鐘", [mk("4GB", part_hint="A"), mk("4GB", part_hint="B")])
    assert estimate_multipart(d).likely == "multi"


def test_lone_part1_marker_is_noise():
    # "-1" alone is a re-encode suffix as often as a first disc.
    d = detail("120分鐘", [mk("4GB", part_hint="-1")])
    assert estimate_multipart(d).likely == "single"


def test_cjk_second_part_marker_is_significant():
    d = detail("120分鐘", [mk("4GB", part_hint="下集")])
    assert estimate_multipart(d).likely == "multi"


def test_letter_beyond_variant_set_never_counts():
    # J+K noise from two different sites must not pair up as variants.
    d = detail("120分鐘", [mk("4GB", part_hint="J"), mk("4GB", part_hint="K")])
    assert estimate_multipart(d).likely == "single"
