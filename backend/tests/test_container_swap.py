"""The swap worklist: codes that landed as a disc image / archive with
no playable video. Presence-index read only — no PikPak, no JavBus."""

import app.services.container_swap as cs


class FakePresence:
    def __init__(self, paths):
        self._paths = paths

    async def get(self, *, force=False):
        return set(self._paths)

    def paths_for(self, code):
        return self._paths.get(code, [])


def _patch(monkeypatch, paths):
    monkeypatch.setattr(cs, "presence_index", FakePresence(paths))


async def test_reports_only_container_only_codes(monkeypatch):
    _patch(monkeypatch, {
        "SNIS-494": ["AVBT/製作商/S1/新人NO.1 STYLE/SNIS-494.iso"],
        "AP-619": ["AVBT/製作商/アパッチ/川の字/AP-619.zip"],
        "MIDV-001": ["AVBT/製作商/S/系/MIDV-001.mp4"],          # fine
        "PPPD-539": ["AVBT/製作商/S/系/PPPD-539.mpg"],          # legacy, playable
    })
    assert await cs.container_only_codes() == [
        {"code": "AP-619", "paths": ["AVBT/製作商/アパッチ/川の字/AP-619.zip"]},
        {"code": "SNIS-494",
         "paths": ["AVBT/製作商/S1/新人NO.1 STYLE/SNIS-494.iso"]},
    ]


async def test_code_mid_swap_is_already_solved(monkeypatch):
    # Replacement landed; the container is just awaiting its sweep.
    # Reporting it would submit the magnet a second time.
    _patch(monkeypatch, {
        "SNIS-494": ["AVBT/製作商/S1/新人/SNIS-494.iso",
                     "AVBT/製作商/S1/新人/SNIS-494.mp4"],
    })
    assert await cs.container_only_codes() == []


async def test_a_bare_code_folder_is_not_a_container(monkeypatch):
    # Legacy presence rows point at 番號 folders, not files. No extension,
    # so nothing to swap — and nothing to mistake for one.
    _patch(monkeypatch, {"OLD-001": ["AVBT/已完成/OLD-001"]})
    assert await cs.container_only_codes() == []
