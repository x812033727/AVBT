"""Phase-1 migrate must not ad-shell-trash a wrapper whose download is
still settling (2026-07-18 audit).

Phase-1 runs over the live TASK folder where offline downloads
materialise. An in-flight file is invisible to the listing, so a wrapper
whose ad clips landed first while the real video is still transferring
reads as an ad shell. Phase-2's winner path already gates this on
is_settling (live TRE-143); phase-1 did not — an actively-downloading
wrapper got diverted to the recycler. Same is_settling gate here.
"""

from types import SimpleNamespace

import app.services.finalize as finalize
import app.services.offline_tasks as offline_tasks
import app.services.reorganize as reorg


def _folder(name, fid):
    return SimpleNamespace(name=name, id=fid, kind="drive#folder")


class _FakePikpak:
    def __init__(self, source_id, children):
        self._source_id = source_id
        self._children = children
        self.trashed: list[str] = []

    async def folder_id(self, path):
        return self._source_id

    async def list_files(self, parent_id, size=500):
        return list(self._children.get(parent_id, []))

    async def trash_files(self, ids):
        self.trashed.extend(ids)
        return {}


async def _run(monkeypatch, *, settling):
    fake = _FakePikpak("src", {"src": [_folder("EDD-138@nyaa", "wrap")]})
    monkeypatch.setattr(reorg, "pikpak_service", fake)

    async def fake_ad_shell(svc, fid):
        return True  # ad clips landed; real video still in flight/invisible

    async def fake_is_settling(fid, *a, **k):
        return settling

    monkeypatch.setattr(finalize, "wrapper_is_ad_shell", fake_ad_shell)
    monkeypatch.setattr(offline_tasks, "is_settling", fake_is_settling)

    events = [
        ev async for ev in reorg._phase1_migrate_from(
            "AVBT/TASK", dry_run=False, idx_start=0)
    ]
    return fake, events


async def test_settling_wrapper_not_ad_shell_trashed(monkeypatch):
    fake, events = await _run(monkeypatch, settling=True)
    assert fake.trashed == []  # in-flight download preserved
    ev = next(e for e in events if e.get("source") == "EDD-138@nyaa")
    assert ev["action"] == "skip" and ev["reason"] == "settling"


async def test_settled_ad_shell_still_trashed(monkeypatch):
    fake, events = await _run(monkeypatch, settling=False)
    assert fake.trashed == ["wrap"]  # genuine aged ad shell
    ev = next(e for e in events if e.get("source") == "EDD-138@nyaa")
    assert ev["action"] == "trash" and ev["reason"] == "ad_shell"
