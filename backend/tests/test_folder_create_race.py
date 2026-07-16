"""Resolving a missing folder path is check-then-create, and PikPak
allows duplicate folder names — so two archives resolving the same absent
path at once both created it. Live 2026-07-16: Aircontrol held "ALL NUDE"
twice, プレステージ held "絶対的美少女、お貸しします。" twice, and
MAS-096.iso sat in the twin no path resolved to, reported as not archived.
"""

import asyncio

from app.services.pikpak import PikPakService


class RacySvc(PikPakService):
    """path_to_id(create=True) with a real await inside it — without one,
    the event loop never interleaves and the race can't be reproduced."""

    def __init__(self):
        super().__init__()
        self.creates: list[str] = []
        self.existing: set[str] = set()

    async def list_all_files(self, parent_id="", *, cap=5000):
        return [], False

    async def lookup_folder_id(self, name):
        return "id-of-" + name if name in self.existing else ""

    async def _call(self, fn):
        # Whatever fn closes over, it is path_to_id(create=True) here.
        path = self._pending_path
        await asyncio.sleep(0)          # yield: the window the race lives in
        self.creates.append(path)
        self.existing.add(path.lstrip("/"))
        return [{"id": "id-of-" + path.lstrip("/")}]

    async def folder_id(self, name):
        self._pending_path = f"/{name}"
        return await super().folder_id(name)


async def test_concurrent_resolves_create_the_folder_once():
    svc = RacySvc()
    path = "AVBT/製作商/エスワン/新人NO.1 STYLE"
    ids = await asyncio.gather(*(svc.folder_id(path) for _ in range(5)))
    assert len(svc.creates) == 1, f"created {len(svc.creates)} times"
    assert len(set(ids)) == 1 and ids[0]


async def test_the_second_caller_gets_the_first_ones_folder():
    svc = RacySvc()
    a, b = await asyncio.gather(
        svc.folder_id("AVBT/製作商/X/系列"),
        svc.folder_id("AVBT/製作商/X/系列"),
    )
    assert a == b == "id-of-AVBT/製作商/X/系列"


async def test_distinct_paths_still_both_get_created():
    # The lock must serialise creation, not prevent it.
    svc = RacySvc()
    await asyncio.gather(
        svc.folder_id("AVBT/製作商/X/系列A"),
        svc.folder_id("AVBT/製作商/X/系列B"),
    )
    assert len(svc.creates) == 2


async def test_a_cached_path_never_reaches_the_lock():
    # Creation is rare and the cache answers everything else; the lock
    # must not become a bottleneck on the hot path.
    svc = RacySvc()
    await svc.folder_id("AVBT/製作商/X/系列")
    async with svc._create_lock:            # held: a cache hit must not wait
        got = await asyncio.wait_for(svc.folder_id("AVBT/製作商/X/系列"), 1)
    assert got == "id-of-AVBT/製作商/X/系列"
