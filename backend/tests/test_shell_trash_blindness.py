"""Shell verdicts (ad shell / empty shell) must fail closed while the
service is blind to PikPak.

"Files landed but not one video" only condemns a folder because anything
still in flight would have shown up in the listing. A login cooldown or
network partition breaks that premise: PikPak keeps writing while we
cannot look, and the age-based gates that exist to catch the misread
(``is_settling`` on row age, ``move_settled`` on move stamps) all expire
for the whole backlog at once during the outage. Live shape: TRE-143 —
a wrapper whose ad clips landed first while the real video was still
transferring, invisible to the listing.
"""

import time
from types import SimpleNamespace

from app.services.finalize import wrapper_is_ad_shell
from app.services.pikpak import SIGHT_STALE_SECONDS, PikPakService

MB = 1024 * 1024


def _file(name, id, size_mb=600):
    return SimpleNamespace(
        name=name, id=id, kind="drive#file", size=size_mb * MB, phase=""
    )


ADS = [_file("1024社區.jpg", "a1", 1), _file("最新地址.txt", "a2", 0)]


class Svc:
    """Listing-only fake, optionally carrying a sight verdict."""

    def __init__(self, graph, *, blind=None):
        self._graph = graph
        if blind is not None:
            self.sight_is_stale = lambda: blind

    async def list_all_files(self, parent_id, *, cap=5000):
        return list(self._graph.get(parent_id, [])), False


# ---------- the guard itself ----------


async def test_blind_service_never_condemns_a_shell():
    assert await wrapper_is_ad_shell(Svc({"w": ADS}, blind=True), "w") is False


async def test_sighted_service_still_condemns_a_shell():
    assert await wrapper_is_ad_shell(Svc({"w": ADS}, blind=False), "w") is True


async def test_service_without_the_probe_reads_as_sighted():
    # Fakes across the existing suite predate the guard; they exercise
    # listing shapes, not outages, and must keep their verdicts.
    assert await wrapper_is_ad_shell(Svc({"w": ADS}), "w") is True


# ---------- PikPakService.sight_is_stale semantics ----------


def _svc(monkeypatch):
    monkeypatch.setenv("PIKPAK_MOVE_LOG", "")
    return PikPakService()


def test_fresh_process_is_stale(monkeypatch):
    # No successful call yet: we have no idea what PikPak did while this
    # process was down, so a restart must not unlock shell trashing.
    assert _svc(monkeypatch).sight_is_stale() is True


def test_first_sight_after_start_still_holds_the_grace(monkeypatch):
    svc = _svc(monkeypatch)
    svc._note_sight()
    assert svc.sight_is_stale() is True


def test_continuous_watching_clears_the_gate(monkeypatch):
    svc = _svc(monkeypatch)
    now = time.time()
    # Regained sight a full grace ago and still seeing PikPak now.
    svc._sight_regained_at = now - SIGHT_STALE_SECONDS - 1
    svc._last_sight = now
    assert svc.sight_is_stale() is False


def test_repeated_sightings_do_not_re_arm_the_grace(monkeypatch):
    """The gate must actually open again, driven only through the public
    stamp. A ``_note_sight`` that re-armed ``_sight_regained_at`` on
    every call would wedge shell cleanup shut forever while every other
    test here stayed green — they all back-date the fields by hand."""
    svc = _svc(monkeypatch)
    svc._note_sight()
    svc._sight_regained_at -= SIGHT_STALE_SECONDS + 1
    # A second call moments later: still watching, nothing to re-arm.
    svc._note_sight()
    assert svc.sight_is_stale() is False


def test_outage_re_arms_the_grace(monkeypatch):
    svc = _svc(monkeypatch)
    now = time.time()
    svc._sight_regained_at = now - 10 * SIGHT_STALE_SECONDS
    svc._last_sight = now - 10 * SIGHT_STALE_SECONDS
    # A cooldown ends and the first call lands: sight is fresh, but the
    # backlog it can now see was written unobserved.
    svc._note_sight()
    assert svc.sight_is_stale() is True


def test_stale_last_sight_alone_is_enough(monkeypatch):
    svc = _svc(monkeypatch)
    now = time.time()
    svc._sight_regained_at = now - 100 * SIGHT_STALE_SECONDS
    svc._last_sight = now - SIGHT_STALE_SECONDS - 1
    assert svc.sight_is_stale() is True
