import asyncio

from app.services.missing import _parallel_map
from app.services.pikpak_presence import PikPakPresenceIndex


async def test_parallel_map_preserves_order_and_bounds_concurrency():
    active = 0
    peak = 0

    async def work(n: int) -> int:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return n * 2

    out = await _parallel_map(list(range(20)), work, limit=3)

    assert out == [n * 2 for n in range(20)]  # order preserved
    assert peak <= 3
    assert peak > 1  # actually ran concurrently


async def test_presence_list_pages_through_large_folders(monkeypatch):
    import app.services.pikpak_presence as pp

    calls: list[str] = []

    async def fake_list_all(parent_id: str = "", *, cap: int = 0):
        calls.append(parent_id)
        return ([object()] * 700, False)  # more than the old 500/page

    monkeypatch.setattr(
        pp.pikpak_service, "list_all_files", fake_list_all, raising=True
    )
    idx = PikPakPresenceIndex()
    files = await idx._list("folder-1")
    assert len(files) == 700
    assert calls == ["folder-1"]


async def test_presence_list_swallows_errors(monkeypatch):
    import app.services.pikpak_presence as pp

    async def boom(parent_id: str = "", *, cap: int = 0):
        raise RuntimeError("nope")

    monkeypatch.setattr(pp.pikpak_service, "list_all_files", boom, raising=True)
    idx = PikPakPresenceIndex()
    assert await idx._list("folder-1") == []
