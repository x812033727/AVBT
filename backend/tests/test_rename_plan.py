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


def test_canonical_scene_zero_padded_hhb():
    # Old-scene DMM ids: zero-padded, HHB disc tag, optional BT dash /
    # numeric prefix. All collapse to the bare code.
    assert _canonical_video_name("SOE00480HHB1.wmv") == "SOE-480"
    assert _canonical_video_name("-SOE00829HHB3.wmv") == "SOE-829"
    assert _canonical_video_name("-49EKDV00246HHB2.wmv") == "EKDV-246"
    assert _canonical_video_name("DVDMS00159HHB1.mp4") == "DVDMS-159"
    # Composite CD<n>-<letter> markers collapse too.
    assert _canonical_video_name("OFJE-296CD1-B.mp4") == "OFJE-296"
    assert _canonical_video_name("OFJE-296CD2-A.mp4") == "OFJE-296"


def test_part_marker_hhb_and_composite_cd():
    assert _part_marker_index("SOE00829HHB3.wmv", "SOE-829") == 3
    assert _part_marker_index("-49EKDV00246HHB2.wmv", "EKDV-246") == 2
    # Composite markers return the disc number; sub-letters tie-break by
    # name in the plan sort.
    assert _part_marker_index("OFJE-296CD1-B.mp4", "OFJE-296") == 1
    assert _part_marker_index("OFJE-296CD2-A.mp4", "OFJE-296") == 2


def test_plan_groups_hhb_discs_as_parts():
    children = [
        _f("-SOE00829HHB3.wmv", int(1.1 * GB)),
        _f("-SOE00829HHB1.wmv", int(1.2 * GB)),
        _f("-SOE00829HHB2.wmv", int(1.1 * GB)),
    ]
    plan, members = _build_video_rename_plan(children, 500 * 1024 * 1024,
                                             _is_video)
    assert plan == {
        "-SOE00829HHB1.wmv": "SOE-829_1.wmv",
        "-SOE00829HHB2.wmv": "SOE-829_2.wmv",
        "-SOE00829HHB3.wmv": "SOE-829_3.wmv",
    }
    assert len(members) == 3


def test_plan_composite_cd_letter_discs_sequential():
    # CD1-A is missing from the set — the four present sub-parts still
    # number consecutively in (disc, letter) order.
    children = [
        _f("OFJE-296CD2-B.mp4", int(2.2 * GB)),
        _f("OFJE-296CD1-C.mp4", int(2.0 * GB)),
        _f("OFJE-296CD2-A.mp4", int(2.3 * GB)),
        _f("OFJE-296CD1-B.mp4", int(2.1 * GB)),
    ]
    plan, _members = _build_video_rename_plan(children, 500 * 1024 * 1024,
                                              _is_video)
    assert plan == {
        "OFJE-296CD1-B.mp4": "OFJE-296_1.mp4",
        "OFJE-296CD1-C.mp4": "OFJE-296_2.mp4",
        "OFJE-296CD2-A.mp4": "OFJE-296_3.mp4",
        "OFJE-296CD2-B.mp4": "OFJE-296_4.mp4",
    }


def test_canonical_trailing_dot_junk():
    # Live loss 2026-07-15: ``DVDMS-445A..mp4`` — the doubled dot leaves
    # a trailing ``.`` on the stem after the extension strip, the
    # end-anchored code match never fires and both discs skip the plan.
    assert _canonical_video_name("DVDMS-445A..mp4") == "DVDMS-445"
    assert _canonical_video_name("DVDMS-446C..mp4") == "DVDMS-446"


def test_canonical_www_suffix_and_paren_resolution():
    # Live case 2026-07-15: ``OAE-314(4K)-WWW.52IV.NET.mkv`` — the site
    # domain tail and the parenthesised resolution both survive the
    # wrapper strip, so the canonical never anchors to the code.
    assert _canonical_video_name("OAE-314(4K)-WWW.52IV.NET.mkv") == "OAE-314"


def test_plan_double_dot_discs_as_parts():
    kids = [_f("DVDMS-445A..mp4", 4 * GB), _f("DVDMS-445B..mp4", 4 * GB)]
    plan, _members = _build_video_rename_plan(kids, 500 * 1024 ** 2, _is_video)
    assert plan == {
        "DVDMS-445A..mp4": "DVDMS-445_1.mp4",
        "DVDMS-445B..mp4": "DVDMS-445_2.mp4",
    }


def test_plan_www_suffixed_single_file_renamed():
    kids = [_f("OAE-314(4K)-WWW.52IV.NET.mkv", 26 * GB)]
    plan, _members = _build_video_rename_plan(kids, 500 * 1024 ** 2, _is_video)
    assert plan == {"OAE-314(4K)-WWW.52IV.NET.mkv": "OAE-314.mkv"}


def test_canonical_site_tag_tail():
    # Live case 2026-07-15: ``gg5.co@435MFC-248-C_GG5.mp4`` — the
    # release-group tag mirrors the stripped ``gg5.co@`` prefix and
    # blocks the end-anchored code match, so the singleton rename kept
    # the junk name. The tag is only stripped when the SAME label was
    # seen as a site prefix on this name.
    assert _canonical_video_name("gg5.co@435MFC-248-C_GG5.mp4") == "MFC-248"
    assert _canonical_video_name("[88Q.ME]GDHH-134_88Q.mp4") == "GDHH-134"
    # No prefix evidence → the tail could be title text and must stay.
    assert _canonical_video_name("MFC-248-C_GG5.mp4") != "MFC-248"
    # Prefix with a different label leaves an unrelated tail alone.
    assert _canonical_video_name("kfa55.com@748SPAY-445.mp4") == "SPAY-445"


def test_plan_site_tag_tail_single_file_renamed():
    kids = [_f("gg5.co@435MFC-248-C_GG5.mp4", 4 * GB)]
    plan, _members = _build_video_rename_plan(kids, 500 * 1024 ** 2, _is_video)
    assert plan == {"gg5.co@435MFC-248-C_GG5.mp4": "MFC-248.mp4"}


def test_canonical_part_markers():
    # Live case 2026-07-15: VR releases split as ``KAVR-497.PART1_.mp4``
    # (trailing underscore included) — the marker was unrecognised, so
    # each part got its own canonical and none were renamed to ``_N``.
    assert _canonical_video_name("KAVR-497.PART1_.mp4") == "KAVR-497"
    assert _canonical_video_name("KAVR-497.PART3_.mp4") == "KAVR-497"
    assert _canonical_video_name("bbs2048.org@oycvr00074.part2.mp4") == "OYCVR-074"
    assert _part_marker_index("KAVR-497.PART2_.mp4", "KAVR-497") == 2
    assert _part_marker_index("bbs2048.org@oycvr00074.part2.mp4", "OYCVR-074") == 2


def test_plan_part_marker_group():
    kids = [
        _f("KAVR-497.PART1_.mp4", 8 * GB),
        _f("KAVR-497.PART2_.mp4", 8 * GB),
        _f("KAVR-497.PART3_.mp4", 7 * GB),
    ]
    plan, _members = _build_video_rename_plan(kids, 500 * 1024 ** 2, _is_video)
    assert plan == {
        "KAVR-497.PART1_.mp4": "KAVR-497_1.mp4",
        "KAVR-497.PART2_.mp4": "KAVR-497_2.mp4",
        "KAVR-497.PART3_.mp4": "KAVR-497_3.mp4",
    }


def test_canonical_bare_domain_tail():
    # Live case 2026-07-15: ``300MIUM-1270-UNCENSORED-NYAP2P.COM.mp4`` —
    # a site-domain tail without the ``WWW.`` prefix survived the strip.
    # Only the final ``<token>.<tld>`` pair goes; the UNCENSORED variant
    # marker is real content information and stays.
    assert (_canonical_video_name("300MIUM-1270-UNCENSORED-NYAP2P.COM.mp4")
            == "300MIUM-1270-UNCENSORED")
    assert _canonical_video_name("FFT-029-nyap2p.com.mp4") == "FFT-029"
    # The WWW.-style tail keeps working through the new rule.
    assert _canonical_video_name("OAE-314(4K)-WWW.52IV.NET.mkv") == "OAE-314"


def test_canonical_codec_tail_and_dangling_dash():
    # Live case 2026-07-15: ``FUN2048.COM - AP752-.mp4`` (dangling dash)
    # and ``hjd2048.com-0819atom387-h264.mp4`` (codec tail). A SPACE-
    # separated codec word could be title text and must not group.
    assert _canonical_video_name("FUN2048.COM - AP752-.mp4") != "FUN2048"
    assert _canonical_video_name("kfa55.com@748SPAY-444-.mp4") == "SPAY-444"
    assert _canonical_video_name("ATOM387-h264.mp4") == "ATOM-387"
    assert (_canonical_video_name("ABC-123 x264.mp4")
            != _canonical_video_name("ABC-123.mp4"))
