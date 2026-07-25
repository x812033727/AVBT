"""The traffic budget must be readable, and one half must not sink the other.

``/quota`` reads storage space (drive/v1/about) and kept reporting a
healthy 81% used while every offline submit was rejected with "Cloud
Download Traffic 40.4 T has exceeded the limit 40 T" — a *different*
budget nothing in the app could see (live 2026-07-18 onwards, 66 hourly
rounds blocked). ``transfer_quota()`` reads both PikPak endpoints that
know about it; since both are undocumented and shaped per account tier,
it must pass payloads through verbatim and degrade one key at a time.
"""

from app.services.pikpak import PikPakService


class FakeClient:
    async def get_transfer_quota(self):
        return {"quantity": [{"kind": "transfer", "limit": "40T", "used": "40.4T"}]}

    async def vip_info(self):
        return {"data": {"expire": "2026-08-01T00:00:00Z", "status": "ok"}}


async def test_transfer_quota_returns_both_payloads_verbatim(monkeypatch):
    svc = PikPakService()

    async def fake_call(op):
        return await op(FakeClient())

    monkeypatch.setattr(svc, "_call", fake_call)

    out = await svc.transfer_quota()

    assert out["transfer"] == {
        "quantity": [{"kind": "transfer", "limit": "40T", "used": "40.4T"}]
    }
    assert out["vip"] == {"data": {"expire": "2026-08-01T00:00:00Z", "status": "ok"}}


async def test_one_failing_endpoint_does_not_sink_the_other(monkeypatch):
    class HalfBrokenClient(FakeClient):
        async def get_transfer_quota(self):
            raise RuntimeError("boom 500")

    svc = PikPakService()

    async def fake_call(op):
        return await op(HalfBrokenClient())

    monkeypatch.setattr(svc, "_call", fake_call)

    out = await svc.transfer_quota()

    assert "boom 500" in out["transfer"]["error"]
    assert out["vip"]["data"]["status"] == "ok"
