"""POST /pikpak/presence/refresh-codes — the per-code escape hatch.

Hand-fixed files (renamed/trashed outside the pipeline) leave those
codes' presence rows stale forever, because refresh_codes otherwise
only runs when a code lands again. The endpoint exposes it on demand
without paying for a full walk.
"""

import pytest
from fastapi import HTTPException

import app.routers.pikpak as pikpak_router


async def test_refresh_codes_calls_index_and_invalidates(monkeypatch):
    seen = {}

    async def fake_refresh(codes, *, exclude_ids=None):
        seen["codes"] = list(codes)
        return 2

    monkeypatch.setattr(
        pikpak_router.presence_index, "refresh_codes", fake_refresh
    )
    import app.services.missing as missing_svc

    invalidated = []
    monkeypatch.setattr(
        missing_svc, "invalidate_all_caches", lambda: invalidated.append(True)
    )

    out = await pikpak_router.presence_refresh_codes(
        codes=["SQTE-656", "EKDV-014"]
    )
    assert out == {"requested": 2, "changed": 2}
    assert seen["codes"] == ["SQTE-656", "EKDV-014"]
    assert invalidated == [True]


async def test_refresh_codes_no_change_skips_invalidate(monkeypatch):
    async def fake_refresh(codes, *, exclude_ids=None):
        return 0

    monkeypatch.setattr(
        pikpak_router.presence_index, "refresh_codes", fake_refresh
    )
    import app.services.missing as missing_svc

    def boom():
        raise AssertionError("must not invalidate when nothing changed")

    monkeypatch.setattr(missing_svc, "invalidate_all_caches", boom)

    out = await pikpak_router.presence_refresh_codes(codes=["ABC-123"])
    assert out == {"requested": 1, "changed": 0}


async def test_refresh_codes_rejects_empty_and_oversized():
    with pytest.raises(HTTPException) as exc:
        await pikpak_router.presence_refresh_codes(codes=[])
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        await pikpak_router.presence_refresh_codes(
            codes=[f"X-{i}" for i in range(51)]
        )
    assert exc.value.status_code == 400
