"""Phase-2 dedupe loser guards. Live loss 2026-08-03: flatten moved
IPVR-326_1.mp4 (3.71GB) out of its wrapper first; -B/-C were still
listing inside when the cleanup pass ran, so the same-code group became
「wrapper folder (summed 6.8GB) vs loose _1 (3.71GB)」 — the folder won
the size contest and the freshly landed part went to the trash as a
"duplicate". The loser branch used to trash regardless of kind and
without a settle gate; these tests pin the three guards that stop it.
"""

from types import SimpleNamespace

import app.services.reorganize as reorg

GB = 1024 ** 3
MB = 1024 ** 2


def _file(name, id_, size, phase="PHASE_TYPE_COMPLETE"):
    return SimpleNamespace(
        id=id_, name=name, kind="drive#file", size=size, phase=phase,
    )


def _folder(name, id_):
    return SimpleNamespace(
        id=id_, name=name, kind="drive#folder", size=None, phase="",
    )


class FakeSvc:
    def __init__(self, graph=None, settled=False):
        self._graph = graph or {}
        self._settled = settled
        self.trashed: list[str] = []
        self.renamed: list[tuple[str, str]] = []
        self.moved: list[tuple[list[str], str]] = []

    async def list_files(self, parent_id, size=100):
        return list(self._graph.get(parent_id, []))

    async def trash_files(self, ids):
        self.trashed.extend(ids)
        return {}

    async def rename_file(self, fid, new_name):
        self.renamed.append((fid, new_name))
        return {}

    async def move_files(self, ids, parent_id):
        self.moved.append((list(ids), parent_id))
        return {}

    def record_move_source(self, source_id):
        pass

    def move_settled(self, source_id):
        return self._settled


async def _run(children, monkeypatch, graph=None, settled=False):
    svc = FakeSvc(graph, settled=settled)
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    events = []
    async for ev in reorg._phase2_cleanup_target(
        "AVBT/製作商/S/系列", "pid-series", children,
        dry_run=False, idx_start=0,
    ):
        events.append(ev)
    return svc, events


async def test_fresh_part_survives_wrapper_contest(monkeypatch):
    """The IPVR-326 shape: a loose substantial part must not lose a
    size contest to the wrapper still holding its siblings."""
    part1 = _file("IPVR-326_1.mp4", "f-part1", int(3.71 * GB))
    wrap = _folder("IPVR-326", "d-wrap")
    graph = {"d-wrap": [
        _file("IPVR-326-B.mp4", "f-b", int(2.33 * GB)),
        _file("IPVR-326-C.mp4", "f-c", int(4.50 * GB)),
    ]}
    svc, events = await _run([part1, wrap], monkeypatch, graph=graph)
    assert "f-part1" not in svc.trashed
    reasons = {e.get("reason") for e in events}
    assert "part_vs_wrapper" in reasons


async def test_loser_folder_with_video_never_trashed(monkeypatch):
    """A folder that lists a video inside must never be the trash-loser
    — the video goes down with it."""
    big = _file("MIRD-100.mp4", "f-big", 9 * GB)
    wrap = _folder("mird-100", "d-wrap")
    graph = {"d-wrap": [_file("mird-100_2.mp4", "f-inner", 2 * GB)]}
    svc, events = await _run([big, wrap], monkeypatch, graph=graph)
    assert "d-wrap" not in svc.trashed
    reasons = {e.get("reason") for e in events}
    assert "loser_folder_has_video" in reasons


async def test_empty_loser_folder_waits_for_settle(monkeypatch):
    """An empty-listing loser folder may be mid-move-out — no trash
    until the settle gate (#140) opens."""
    big = _file("MIRD-101.mp4", "f-big", 9 * GB)
    shell = _folder("mird-101", "d-shell")
    svc, events = await _run([big, shell], monkeypatch)
    assert "d-shell" not in svc.trashed
    reasons = {e.get("reason") for e in events}
    assert "move_settling" in reasons


async def test_settled_empty_loser_folder_still_trashed(monkeypatch):
    """Once settled, an empty shell loser retires as before."""
    big = _file("MIRD-102.mp4", "f-big", 9 * GB)
    shell = _folder("mird-102", "d-shell")
    svc, _ = await _run([big, shell], monkeypatch, settled=True)
    assert "d-shell" in svc.trashed


async def test_small_video_loser_to_folder_still_trashed(monkeypatch):
    """A sub-500MB clip losing to a folder is sample junk, not a part —
    dedupe takes it as before."""
    wrap = _folder("mird-103", "d-wrap")
    clip = _file("MIRD-103.mp4", "f-clip", 200 * MB)
    graph = {"d-wrap": [_file("mird-103.mp4", "f-inner", 5 * GB)]}
    svc, _ = await _run([wrap, clip], monkeypatch, graph=graph)
    assert "f-clip" in svc.trashed


async def test_file_vs_file_dedupe_unchanged(monkeypatch):
    """Plain video-vs-video duplicate handling is untouched."""
    big = _file("SONE-092.mp4", "f-big", 26 * GB)
    small = _file("SONE-092(1).mp4", "f-small", 8 * GB)
    svc, _ = await _run([big, small], monkeypatch)
    assert svc.trashed == ["f-small"]
