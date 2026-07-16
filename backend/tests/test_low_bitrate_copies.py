"""Runtime settles what size cannot: two videos of the same length are
the same film, however different their bitrates.

Every group below is real, measured on 2026-07-16. The rota had been
stuck for rounds on the "_5 家族" (GDHH-167 / CLUB-512 / CLVR-075),
flagged "勿硬壓連號" because nothing could tell a fake _5 from a real
one. Two of the three are re-encodes and one is a genuine part set —
the runtimes say so in a second.
"""

from types import SimpleNamespace

import pytest

from app.services.jav_code import is_video
from app.services.rename_plan import _build_video_rename_plan, low_bitrate_copies

MB = 1024 * 1024


def _v(name, minutes, gb):
    return SimpleNamespace(name=name, duration=minutes * 60, size=int(gb * 1e9),
                           kind="drive#file", id=name)


def test_a_re_encode_beside_the_real_thing():
    # GDHH-167: identical 195-min runtime, a seventh of the size, and it
    # had been sitting on disk as a "_5 disc" for weeks.
    files = [_v("GDHH-167_1.mp4", 195, 6.04), _v("GDHH-167_5.mp4", 195, 0.91)]
    assert [c.name for c in low_bitrate_copies(files)] == ["GDHH-167_5.mp4"]


def test_three_bitrates_of_one_film():
    # CLUB-561: 219/221/221 min at 10.2/6.85/1.02GB. Only the obvious one
    # is reported — 6.85GB against 10.2GB is a judgement call, and this
    # function does not make those.
    files = [_v("CLUB-561.mp4", 219, 10.2),
             _v("HJD2048.COM-0528CLUB561-H264.mp4", 221, 6.85),
             _v("CLUB561-5.mp4", 221, 1.02)]
    assert [c.name for c in low_bitrate_copies(files)] == ["CLUB561-5.mp4"]


def test_six_real_discs_are_untouched():
    # OFJE-276, a best-of omnibus: six discs, each a ~2h compilation.
    files = [_v("OFJE-276CD1-A.mp4", 120, 5.31), _v("OFJE-276CD1-B.mp4", 119, 5.26),
             _v("OFJE-276CD1-C.mp4", 116, 5.15), _v("OFJE-276CD2-A.mp4", 123, 5.46),
             _v("OFJE-276CD2-B.mp4", 122, 5.43), _v("OFJE-276CD2-C.mp4", 115, 5.12)]
    assert low_bitrate_copies(files) == []


def test_six_real_discs_survive_even_if_their_runtimes_cluster():
    # The duration gate alone would fail here — 118-122 min is inside the
    # 5% tolerance. The size gate is what makes this safe to automate:
    # real discs are all about as big as each other.
    files = [_v("OFJE-276CD1-A.mp4", 120, 5.31), _v("OFJE-276CD1-B.mp4", 119, 5.26),
             _v("OFJE-276CD1-C.mp4", 118, 5.15), _v("OFJE-276CD2-A.mp4", 122, 5.46),
             _v("OFJE-276CD2-B.mp4", 121, 5.43), _v("OFJE-276CD2-C.mp4", 118, 5.12)]
    assert low_bitrate_copies(files) == []


def test_a_real_part_set_of_short_clips():
    # CLVR-075: 16/22/24/11 min — the one genuine member of the _5 家族.
    files = [_v("CLVR-075_1.mp4", 16, 4.05), _v("CLVR-075_2.mp4", 22, 5.41),
             _v("CLVR-075_3.mp4", 24, 5.33), _v("CLVR-075_5.mp4", 11, 2.76)]
    assert low_bitrate_copies(files) == []


def test_same_size_copies_are_left_to_a_human():
    # STOL-094: 239 min / 11.01GB beside 236 min / 10.65GB. Both copies of
    # one film, but nothing here can prove it isn't a two-disc release.
    files = [_v("STOL-094.mp4", 239, 11.01), _v("STOL-094_2 (2).mp4", 236, 10.65)]
    assert low_bitrate_copies(files) == []


@pytest.mark.parametrize(
    "files",
    [
        # An unprobed file must never be judged...
        [_v("A-1_1.mp4", 100, 6.0), _v("A-1_2.mp4", 0, 0.9)],
        # ...nor a group PikPak knows nothing about.
        [_v("A-1_1.mp4", 0, 6.0), _v("A-1_2.mp4", 0, 0.9)],
    ],
)
def test_unknown_runtime_is_never_judged(files):
    assert low_bitrate_copies(files) == []


def test_the_plan_hands_a_re_encode_to_the_dedup():
    # The point of all this: a fake _5 must stop claiming a part slot, so
    # the winner-based dedup can retire it.
    plan, members = _build_video_rename_plan(
        [_v("GDHH-167_1.mp4", 195, 6.04), _v("GDHH-167_5.mp4", 195, 0.91)],
        500 * MB, is_video, require_marker=True,
    )
    assert plan == {} and members == set()


def test_the_plan_still_numbers_real_discs():
    plan, _m = _build_video_rename_plan(
        [_v("OFJE-276CD1-A.mp4", 120, 5.31), _v("OFJE-276CD2-A.mp4", 123, 5.46)],
        500 * MB, is_video, require_marker=True,
    )
    assert sorted(plan.values()) == ["OFJE-276_1.mp4", "OFJE-276_2.mp4"]
