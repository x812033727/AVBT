import httpx

from app.services.pcloud import PCloudService


async def test_client_is_reused_across_calls():
    svc = PCloudService()
    c1 = await svc._get_client()
    c2 = await svc._get_client()
    assert c1 is c2
    assert isinstance(c1, httpx.AsyncClient)
    await svc.aclose()
    assert c1.is_closed


async def test_closed_client_is_rebuilt():
    svc = PCloudService()
    c1 = await svc._get_client()
    await svc.aclose()
    c2 = await svc._get_client()
    assert c2 is not c1
    assert not c2.is_closed
    await svc.aclose()


async def test_raw_request_uses_shared_client(monkeypatch):
    svc = PCloudService()
    calls: list[str] = []

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": 0, "ok": True}

    class _FakeClient:
        is_closed = False

        async def get(self, url, params=None, timeout=None):
            calls.append(url)
            return _FakeResp()

        async def post(self, url, data=None, timeout=None):
            calls.append(url)
            return _FakeResp()

    svc._client = _FakeClient()
    data = await svc._raw_request("https://api.pcloud.com", "userinfo", {})
    assert data["ok"] is True
    data = await svc._raw_request("https://api.pcloud.com", "listfolder", {})
    assert len(calls) == 2  # both went through the injected shared client
