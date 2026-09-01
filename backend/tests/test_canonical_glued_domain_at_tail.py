"""Site tag glued onto the code's tail and closed with ``@``
(``AP317Czzpp08.com@.mp4``): the at-prefix strip reads the whole stem as
``<site>@`` and the canonical collapses to "" — the plan then renamed
AP-317's only copy to a bare ``.mp4`` (live 2026-09-01)."""

from types import SimpleNamespace

from app.services.rename_plan import _build_video_rename_plan, _canonical_video_name

GB = 1024 ** 3


def _f(name: str, size: int = 2 * GB):
    return SimpleNamespace(name=name, size=size, kind="drive#file")


def _is_video(name: str) -> bool:
    return name.lower().endswith((".mp4", ".mkv", ".avi", ".wmv"))


def test_canonical_glued_domain_at_tail_is_the_code():
    assert _canonical_video_name("AP317Czzpp08.com@.mp4") == "AP-317"
    # separator-led tails stay with the existing rule (UNCENSORED kept)
    assert (_canonical_video_name("300MIUM-1270-UNCENSORED-NYAP2P.COM.mp4")
            == "300MIUM-1270-UNCENSORED")
    assert _canonical_video_name("AP317Czzpp08.com.mp4") == "AP-317"
    # the leading-site form still works as before
    assert _canonical_video_name("zzpp08.com@AP317C.mp4") == "AP-317"


def test_canonical_never_empty():
    # All-noise stems must not collapse to "" — fall back to the raw stem.
    assert _canonical_video_name("zzpp08.com@.mp4") != ""
    assert _canonical_video_name("[88K.ME].mp4") != ""


def test_plan_never_targets_a_bare_extension():
    plan, _ = _build_video_rename_plan(
        [_f("AP317Czzpp08.com@.mp4", 1_699_203_827)], 300 * 1024 * 1024,
        _is_video, require_marker=True,
    )
    assert plan == {"AP317Czzpp08.com@.mp4": "AP-317.mp4"}
    plan, _ = _build_video_rename_plan(
        [_f("zzpp08.com@.mp4")], 300 * 1024 * 1024, _is_video, require_marker=True,
    )
    assert all(t.rsplit(".", 1)[0] for t in plan.values())
