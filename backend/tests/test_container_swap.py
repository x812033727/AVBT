"""The swap worklist: codes that landed as a disc image / archive with
no playable video. Presence-index read only — no PikPak, no JavBus."""

import app.services.container_swap as cs


class FakePresence:
    """``on_refresh`` stands in for PikPak: it replaces a code's paths the
    way a live re-read would, so a test can make the index stale."""

    def __init__(self, paths, on_refresh=None, fail=False):
        self._paths = paths
        self._on_refresh = on_refresh or {}
        self._fail = fail
        self.refreshed: list[str] = []

    async def get(self, *, force=False):
        return set(self._paths)

    def paths_for(self, code):
        return self._paths.get(code, [])

    async def refresh_codes(self, codes, *, exclude_ids=None):
        if self._fail:
            raise RuntimeError("pikpak down")
        self.refreshed.extend(codes)
        for c in codes:
            if c in self._on_refresh:
                self._paths[c] = self._on_refresh[c]
        return len(codes)


def _patch(monkeypatch, paths, **kw):
    fake = FakePresence(paths, **kw)
    monkeypatch.setattr(cs, "presence_index", fake)
    return fake


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


async def test_a_stale_row_is_dropped_not_swapped(monkeypatch):
    # The index lags: it still listed AP-619.zip and MAS-096.iso after
    # both were gone, and each phantom cost a download. Only the live
    # re-read decides.
    fake = _patch(
        monkeypatch,
        {"AP-619": ["AVBT/製作商/アパッチ/川の字/AP-619.zip"],
         "SNIS-494": ["AVBT/製作商/S1/新人/SNIS-494.iso"]},
        on_refresh={"AP-619": ["AVBT/製作商/アパッチ/川の字/AP-619.mp4"]},
    )
    assert await cs.container_only_codes() == [
        {"code": "SNIS-494", "paths": ["AVBT/製作商/S1/新人/SNIS-494.iso"]},
    ]
    # Only the candidates get re-read — never the whole library.
    assert sorted(fake.refreshed) == ["AP-619", "SNIS-494"]


async def test_verify_off_skips_the_live_read(monkeypatch):
    fake = _patch(monkeypatch, {"AP-619": ["AVBT/製作商/A/B/AP-619.zip"]})
    assert len(await cs.container_only_codes(verify=False)) == 1
    assert fake.refreshed == []


async def test_pikpak_failure_reports_unverified_rather_than_nothing(monkeypatch):
    # A stale answer beats no answer: the worker's own MAX_ATTEMPTS and
    # 409 dedupe bound what a wrong row can cost.
    _patch(monkeypatch, {"SNIS-494": ["AVBT/製作商/S1/新人/SNIS-494.iso"]},
           fail=True)
    assert len(await cs.container_only_codes()) == 1
