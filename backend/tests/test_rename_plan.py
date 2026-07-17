"""Canonical-name / part-marker blind spots that defeated multi-disc
grouping (live loss 2026-07-14: HUNTA-578 disc B trashed as a
"duplicate" of disc A because the hyphen-less, ext-glued spelling
``HUNTA578AMP4`` never anchored to the code)."""

from types import SimpleNamespace

from app.services.rename_plan import (
    _build_video_rename_plan,
    _canonical_video_name,
    _part_marker_index,
    quality_tagged_copies,
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


def test_canonical_bracket_quality_tag():
    # ``_[4K]`` — bracketed quality tail; ``_DUP_SUFFIX_RE`` only knew
    # the bare and parenthesised spellings, so the tail survived and
    # blocked the code anchor (live: RCTD 4K batch archived as
    # ``RCTD-697_[4K].mkv``, 2026-07-17).
    assert _canonical_video_name("RCTD-697_[4K].mkv") == "RCTD-697"
    assert _canonical_video_name("RCTD-688_[4K].mkv") == "RCTD-688"
    assert _canonical_video_name("ABC-123[1080P].mp4") == "ABC-123"
    # The bracket tail is an encode name, never a part marker.
    assert _part_marker_index("RCTD-697_[4K].mkv", "RCTD-697") == 0


def test_canonical_dotted_scene_tail():
    # Old-scene dotted release tail glued after the code (live:
    # ``RCT-208.DL.XN-FP.wmv`` archived verbatim, 2026-07-17). Every
    # token is dot-separated, short and carries a letter — site/release
    # noise, never title text.
    assert _canonical_video_name("RCT-208.DL.XN-FP.wmv") == "RCT-208"
    # A dotted numeric tail could be a part slot — stays untouched.
    assert _canonical_video_name("ABC-123.2.mp4") != "ABC-123"
    # A dotted lone letter could be a variant/disc — stays untouched.
    assert _canonical_video_name("ABC-123.A.mp4") != "ABC-123"
    # Space-separated title text keeps its distinct canonical.
    assert _canonical_video_name("MIDV-001 making-of.mp4") != "MIDV-001"


def test_bracket_quality_tagged_copy_dropped_from_group():
    # ``CODE_[4K]`` beside the old bare rip must not claim a fake part
    # slot — the tagged member drops to the keep-the-biggest dedup.
    files = [_f("RCTD-697.mp4", size=2 * GB),
             _f("RCTD-697_[4K].mkv", size=8 * GB)]
    copies = quality_tagged_copies(files, "RCTD-697")
    assert [f.name for f in copies] == ["RCTD-697_[4K].mkv"]


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


def test_part_marker_resolution_tail_next_to_code():
    # A resolution/quality tail hanging off the code is not a part index:
    # the fallback's ``\d{1,2}`` guard must hold for the main regex too.
    assert _part_marker_index("GDHH-167-1080p.mp4", "GDHH-167") == 0
    assert _part_marker_index("GDHH-167_1080p.mp4", "GDHH-167") == 0
    assert _part_marker_index("ABC-123-720p.mp4", "ABC-123") == 0
    assert _part_marker_index("ABC-123-4K.mp4", "ABC-123") == 0
    # Real single-digit part markers next to the code still count.
    assert _part_marker_index("GDHH-167-2.mp4", "GDHH-167") == 2
    assert _part_marker_index("ABC-123_3.mp4", "ABC-123") == 3


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


def test_canonical_glued_domain_head():
    # Live case 2026-07-16: ``hhd800.comHRSM-130.mp4`` — the site domain
    # sits at the HEAD glued straight onto the code. The ``@``/bracket
    # prefix rules never fire (no separator) and the domain rules are
    # end-anchored, so the host's ``COM`` fused onto the code and the
    # file landed as ``COMHRSM-130.mp4``: presence lost HRSM-130 and the
    # task row never finalized, stranding an empty wrapper folder.
    assert _canonical_video_name("hhd800.comHRSM-130.mp4") == "HRSM-130"
    assert _canonical_video_name("carib.com010112-123.mp4") == "010112-123"
    # Multi-label host: every ``<token>.`` up to the tld is site noise.
    assert _canonical_video_name("www.hhd800.comHRSM-129.mp4") == "HRSM-129"
    # A head token whose tail is not a real tld is content, not a host —
    # FC2 PPV codes must survive untouched.
    assert _canonical_video_name("FC2.PPV-1234567.mp4") == "FC2.PPV-1234567"
    # Separator forms already resolved through other rules — no regress.
    assert _canonical_video_name("hhd800.com@HRSM-130.mp4") == "HRSM-130"
    assert _canonical_video_name("hhd800.com-HRSM-130.mp4") == "HRSM-130"
    assert _canonical_video_name("hjd2048.com-0819atom387-h264.mp4") == "ATOM-387"


def test_plan_glued_domain_head_single_file_renamed():
    kids = [_f("hhd800.comHRSM-130.mp4", 10 * GB)]
    plan, _members = _build_video_rename_plan(kids, 500 * 1024 ** 2, _is_video)
    assert plan == {"hhd800.comHRSM-130.mp4": "HRSM-130.mp4"}


def test_canonical_codec_tail_and_dangling_dash():
    # Live case 2026-07-15: ``FUN2048.COM - AP752-.mp4`` (dangling dash)
    # and ``hjd2048.com-0819atom387-h264.mp4`` (codec tail). A SPACE-
    # separated codec word could be title text and must not group.
    assert _canonical_video_name("FUN2048.COM - AP752-.mp4") != "FUN2048"
    assert _canonical_video_name("kfa55.com@748SPAY-444-.mp4") == "SPAY-444"
    assert _canonical_video_name("ATOM387-h264.mp4") == "ATOM-387"
    assert (_canonical_video_name("ABC-123 x264.mp4")
            != _canonical_video_name("ABC-123.mp4"))


def test_sd_rip_never_claims_a_part_slot_in_wrapper_scope():
    # Live case 2026-07-16 (KBTK-012): a same-torrent SD rip at 62% of
    # the HD file. Durations unprobed and >½ the size, so neither
    # low_bitrate_copies nor _split_size_outliers can judge it — only
    # the name gives it away. Without the tag rule the pair became a
    # fake ``_1``/``_2`` (the exact mine that reverted PR #182).
    kids = [
        _f("KBTK-012.mp4", int(4.56 * GB)),
        _f("KBTK-012-SD.mp4", int(2.85 * GB)),
    ]
    plan, members = _build_video_rename_plan(kids, 500 * 1024 ** 2, _is_video)
    assert plan == {}      # no fake multipart pair
    assert members == set()  # both stay visible to the keep-biggest dedup


def test_sd_rip_series_scope_left_to_dedup():
    # SKMJ-480 — the pair that got PR #182 reverted. In a 系列 folder
    # (require_marker=True) neither file carries a disc marker, so the
    # group must fall through to the dedup, not become ``_1``/``_2``.
    kids = [
        _f("SKMJ-480.mp4", int(7.4 * GB)),
        _f("SKMJ-480-SD.mp4", int(2.41 * GB)),
    ]
    plan, members = _build_video_rename_plan(
        kids, 500 * 1024 ** 2, _is_video, require_marker=True
    )
    assert plan == {}
    assert members == set()


def test_quality_tag_with_part_marker_still_a_disc():
    # ``CODE-HD_1`` / ``CODE-HD_2`` — discs of an HD encode. The tag
    # rule must not steal marker-bearing files from the group.
    kids = [
        _f("ABC-123-HD_1.mp4", 5 * GB),
        _f("ABC-123-HD_2.mp4", 5 * GB),
    ]
    assert quality_tagged_copies(kids, "ABC-123") == []
    plan, members = _build_video_rename_plan(kids, 500 * 1024 ** 2, _is_video)
    assert plan == {
        "ABC-123-HD_1.mp4": "ABC-123_1.mp4",
        "ABC-123-HD_2.mp4": "ABC-123_2.mp4",
    }
    assert members == {"ABC-123-HD_1.mp4", "ABC-123-HD_2.mp4"}


def test_quality_tag_collision_pair_still_discs():
    # Same-name collision inside one torrent means discs even when the
    # shared name carries a tag — ``(N)`` marks the collision, not a
    # re-encode, so the pair keeps its slots.
    kids = [
        _f("SKMJ-480-SD.mp4", 3 * GB),
        _f("SKMJ-480-SD (2).mp4", 3 * GB),
    ]
    assert quality_tagged_copies(kids, "SKMJ-480") == []


def test_canonical_glued_quality_part_tail():
    # ``SQTE-645_4KS1`` — quality tag glued between separator and part
    # index. The tag hid the digit from _DUP_SUFFIX_RE and the digit hid
    # the tag from the part regexes, so the name canonicalised to itself
    # and archived verbatim (live 2026-07-17).
    assert _canonical_video_name("SQTE-645_4KS1.mp4") == "SQTE-645"
    assert _canonical_video_name("SQTE-645_4KS2.mp4") == "SQTE-645"
    assert _part_marker_index("SQTE-645_4KS1.mp4", "SQTE-645") == 1
    assert _part_marker_index("SQTE-645_4KS2.mp4", "SQTE-645") == 2
    # A bare tag with no trailing index is untouched by the glue rule.
    assert _canonical_video_name("4k2.me@sqte-656-4k.mp4") == "SQTE-656"
    assert _part_marker_index("4k2.me@sqte-656-4k.mp4", "SQTE-656") == 0


def test_plan_glued_quality_part_group():
    kids = [
        _f("SQTE-645_4KS1.mp4", int(11.7 * GB)),
        _f("SQTE-645_4KS2.mp4", int(10.4 * GB)),
    ]
    plan, members = _build_video_rename_plan(kids, 500 * 1024 ** 2, _is_video)
    assert plan == {
        "SQTE-645_4KS1.mp4": "SQTE-645_1.mp4",
        "SQTE-645_4KS2.mp4": "SQTE-645_2.mp4",
    }
    assert members == {"SQTE-645_4KS1.mp4", "SQTE-645_4KS2.mp4"}


def test_plan_collapsed_group_survivor_still_renamed():
    # Bare HD beside its declared SD rip (KBTK-012 shape): the SD copy
    # goes to the dedup, but the surviving biggest file must still get
    # its canonical singleton name instead of keeping BT noise.
    kids = [
        _f("[88K.ME]KBTK-012.mp4", int(4.56 * GB)),
        _f("[88K.ME]KBTK-012-SD.mp4", int(2.85 * GB)),
    ]
    plan, members = _build_video_rename_plan(kids, 500 * 1024 ** 2, _is_video)
    assert plan == {"[88K.ME]KBTK-012.mp4": "KBTK-012.mp4"}
    assert members == set()


def test_plan_collapsed_group_smaller_survivor_left_alone():
    # The tagged file is the BIGGER one (bare-SD beside its ``-4k``
    # upgrade, SQTE-656 shape): crowning the smaller survivor with the
    # canonical name would hand the identity to the loser — leave the
    # group to the caller's keep-the-biggest dedup.
    kids = [
        _f("4k2.me@sqte-656.mp4", int(4.61 * GB)),
        _f("4k2.me@sqte-656-4k.mp4", int(7.13 * GB)),
    ]
    plan, members = _build_video_rename_plan(kids, 500 * 1024 ** 2, _is_video)
    assert plan == {}
    assert members == set()
