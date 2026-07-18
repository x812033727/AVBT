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
        # Old-scene zero-padded DMM ids with the HHB disc tag
        ("SOE00480HHB1.wmv", "SOE-480"),
        ("-SOE00829HHB3.wmv", "SOE-829"),
        ("soe00877hhb2.wmv", "SOE-877"),
        ("DVDMS00159HHB1.mp4", "DVDMS-159"),
        # …with a BT numeric prefix in front
        ("-49EKDV00246HHB2.wmv", "EKDV-246"),
        # Glued CD disc marker, with and without a sub-part letter
        ("OFJE-296CD1-B.mp4", "OFJE-296"),
        ("OFJE-296CD2.mp4", "OFJE-296"),
        # Zero padding collapses even without a marker
        ("SOE-00480.wmv", "SOE-480"),
        ("soe00048.wmv", "SOE-048"),
        # 4-digit leading zeros are genuine (HEYZO style) — untouched
        ("HEYZO-0123.mp4", "HEYZO-0123"),
        # Ordinary 3-digit padding untouched
        ("SONE-001.mkv", "SONE-001"),
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
        # Scene disc markers are consumed, not mistaken for variants
        ("SOE00829HHB3.wmv", "SOE-829"),
        ("OFJE-296CD1-B.mp4", "OFJE-296"),
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
    # Trailing ASCII dots are illegal folder names on PikPak — strip them
    # (the 働くドMさん. series could never be archived). Full-width 。 is
    # accepted by PikPak and must survive (港区女子。).
    assert safe_folder_name("働くドMさん.") == "働くドMさん"
    assert safe_folder_name("name..") == "name"
    assert safe_folder_name("港区女子。") == "港区女子。"
    assert safe_folder_name("...", fallback="fb") == "fb"
    # Truncation to 64 must not leave a trailing dot behind either.
    assert safe_folder_name("x" * 63 + "..") == "x" * 63


def test_ext_and_is_video():
    assert ext_of("movie.MP4") == ".mp4"
    assert ext_of("noext") == ""
    assert is_video("a.mkv") is True
    assert is_video("a.jpg") is False


def test_legacy_containers_are_video():
    # finalize permanently purges "non-video" files — a missing legacy
    # container here means a keeper is one cleanup away from deletion.
    for ext in (".mpg", ".mpeg", ".rmvb", ".m2ts", ".vob",
                ".asf", ".rm", ".divx", ".ogm"):
        assert is_video(f"PPPD-539{ext}") is True, ext
    assert extract_jav_code("PPPD-539MPG") == "PPPD-539"


# ---------------------------------------------------------------------------
# Site-noise pre-strip (#182-hazard: CANONICAL-PARSING change).
#
# BT wrapper names glue a site tag onto the real code in three shapes:
# [bracket], user@ (one or more chained), and a bare host.tld token. All
# three must be stripped BEFORE the code scan runs, so a domain that has
# NO real code in it at all (``hjd2048.com``) can't have its own
# digits+letters misread as a squished code (the ``HJD-2048`` ghost).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("name", "want"),
    [
        # [bracket] site tag
        ("[44x.me]gapl-015", "GAPL-015"),
        ("[88K.ME]TRE-112", "TRE-112"),
        # user@ site tag, single and chained (multi-layer)
        ("ccc11.cc@230ORECZ-510J", "ORECZ-510"),
        ("kfa55.com@483DAM-043", "DAM-043"),
        ("a.b@c.d@CODE-123", "CODE-123"),
        # Bare host.tld token, space-separated from the code
        ("hjd2048.com GAPL-015", "GAPL-015"),
        ("kan224.com ABP-543", "ABP-543"),
        # Digit-domain glue: "44" reads as extract_jav_code's existing
        # numeric-prefix noise (300MIUM/259LUXU-style stripping, out of
        # scope for this feature) once "test.la@" is stripped off — pinned
        # as-is, not a behavior change from this feature.
        ("test.la@44ID-026", "ID-026"),
    ],
)
def test_extract_jav_code_strips_site_noise(name, want):
    assert extract_jav_code(name) == want


@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("[44x.me]gapl-015", "GAPL-015"),
        ("ccc11.cc@230ORECZ-510J", "ORECZ-510J"),
        ("a.b@c.d@CODE-123", "CODE-123"),
        ("hjd2048.com GAPL-015", "GAPL-015"),
    ],
)
def test_extract_jav_code_full_strips_site_noise(name, want):
    assert extract_jav_code_full(name) == want


@pytest.mark.parametrize(
    "name",
    [
        # Domain-only wrapper names carry NO real code anywhere — must
        # return None, never mint a fabricated code from the domain's
        # own digits+letters.
        "hjd2048.com",
        "kan224.com",
        "hjd2048.net",
        "[44x.me]",
    ],
)
def test_extract_jav_code_domain_only_is_none(name):
    assert extract_jav_code(name) is None
    assert extract_jav_code_full(name) is None


# #182-class negatives: site-noise stripping must not change extraction
# of names that already work today — byte-identical before vs after this
# feature (values captured from the pre-change extractor).
@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("HRSM-032-SD", "HRSM-032"),
        ("HRSM-032", "HRSM-032"),
        ("ABC-123A", "ABC-123"),
        ("ABC-123B", "ABC-123"),
        ("OFJE-296CD1-B", "OFJE-296"),
        ("SOE00829HHB3", "SOE-829"),
        ("300MIUM-1098", "MIUM-1098"),
        ("259LUXU-1543", "LUXU-1543"),
    ],
)
def test_extract_jav_code_site_noise_byte_identical_negatives(name, want):
    assert extract_jav_code(name) == want


# Corpus-evidence-driven guards (round4-prB): the corpus run against every
# live presence_entry / offline_task_log name proved that stripping a
# ``[bracket]`` tag or a ``user@`` prefix unconditionally (mirroring
# rename_plan.py's _BT_PREFIX_BRACKET_RE / _BT_PREFIX_AT_RE) destroys real
# codes that legitimately sit in that position — a "[CODE] title" or
# "CODE@uploader-tag" wrapper is just as common as "[SITE]CODE" /
# "site@CODE" in this corpus, and _CODE_RE's own boundary check already
# gets the former right without any stripping. So _strip_site_noise
# implements ONLY the bare-domain-token rule, not bracket/at.
@pytest.mark.parametrize(
    ("name", "want"),
    [
        # [CODE] title — bracket wraps the real code, not a site tag.
        ("[CLUB-044] Title", "CLUB-044"),
        ("[HUNTA-594] some title text", "HUNTA-594"),
        # CODE@uploader-tag — @ separates the code from a release-group /
        # uploader label, not a site name from the code.
        ("ATOM-035@oldman", "ATOM-035"),
        ("(DVD)EKDV-038@wolf", "EKDV-038"),
    ],
)
def test_extract_jav_code_bracket_and_at_are_not_stripped(name, want):
    assert extract_jav_code(name) == want


def test_extract_jav_code_domain_tld_label_collision_guard():
    # "CLUB" is both a curated site TLD and a real JAV label prefix
    # (CLUB-032, CLUB-044, …). The domain rule's lookahead requires
    # whitespace/@//  /end right after the TLD — NOT a hyphen — so a real
    # code like ``122.CLUB-032`` (BT numeric-prefix glue + the label
    # CLUB-032) is never mistaken for a ``122.club`` domain.
    assert extract_jav_code("122.club-032") == "CLUB-032"


def test_dmm_poster_suffix_stripped():
    # Torrents named after DMM cover art keep its ``pl`` (package-large)
    # tail glued to the content id — the wrapper must still parse or the
    # sweep leaves it in AVBT/TASK forever (live: OYCVR-058).
    assert extract_jav_code("oycvr00058pl") == "OYCVR-058"
    assert extract_jav_code("oycvr-058pl") == "OYCVR-058"
    assert extract_jav_code_full("oycvr00058pl") == "OYCVR-058"
    # Variant letter before the poster tail still collapses to base.
    assert extract_jav_code("abp-123apl") == "ABP-123"
    # Guards: existing suffix forms keep working.
    assert extract_jav_code("SNOS-015ch.mp4") == "SNOS-015"
    assert extract_jav_code("OFJE-296CD1-B") == "OFJE-296"


# ---------------------------------------------------------------------------
# Quality tag vs different cut — _canonical_video_name's dividing line
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("name", "want"),
    [
        # Same content, worse/other encode: strip, so the pair groups and
        # the dedupe keeps the bigger. Live 2026-07-16: these survived
        # every pass because the tag list missed them.
        ("HRSM-032-SD.mp4", "HRSM-032"),
        ("SKMJ-480-SD.mp4", "SKMJ-480"),
        ("SQTE-659_4KS.mp4", "SQTE-659"),
        ("RCTD-733_4KS.mp4", "RCTD-733"),
        # Codec tag with a dot separator — the rule only knew -/_ .
        ("MBRBA-121.H265.mp4", "MBRBA-121"),
        ("SQTE-660-.mp4", "SQTE-660"),
        # Already covered; guard against the reorder breaking them.
        ("TRE-112 HD.mp4", "TRE-112"),
        ("OAE-314(4K).mp4", "OAE-314"),
        ("SNOS-015ch.mp4", "SNOS-015"),
    ],
)
def test_quality_tags_are_stripped_from_the_canonical(name, want):
    from app.services.rename_plan import _canonical_video_name

    assert _canonical_video_name(name) == want


@pytest.mark.parametrize(
    "name",
    [
        # A different CUT, not a different encode. Stripping these groups
        # an uncensored rip with the censored retail release — and the
        # dedupe keeps the BIGGER file, which is usually the censored one.
        # The uncensored copy would be trashed to "deduplicate" it.
        "PKYS-019_UNC.mp4",
        "CAWD-957-UC.mp4",
        "FFT-029-UNCENSORED.mp4",
        "300MIUM-1242-UNCENSORED.mp4",
        "FFT-019-AI.mp4",          # AI-decensored
    ],
)
def test_a_different_cut_keeps_its_marker(name):
    from app.services.rename_plan import _canonical_video_name

    stem = name.rsplit(".", 1)[0]
    assert _canonical_video_name(name) == stem.upper()
