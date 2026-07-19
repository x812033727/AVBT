"""Phase-2 dedupe must never trash a playable video as a "duplicate" of
a container (.iso/.rar/…). Live loss 2026-07-18: OAE-173.rar (4.6GB) sat
at the canonical name, and every replacement video the container swap
landed lost the pure size contest and went to the trash — three real
videos in one day, while the rar never retired. The dedupe now ranks
playable candidates above containers and retires the container instead,
behind the same credibility bar series_junk uses (a replacement below
25% of the container is a downgrade — both stay for a human)."""

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
    def __init__(self, graph=None):
        self._graph = graph or {}
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
        return False  # fresh moves never settle inside one test run


async def _run(children, monkeypatch, graph=None):
    svc = FakeSvc(graph)
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    events = []
    async for ev in reorg._phase2_cleanup_target(
        "AVBT/製作商/S/系列", "pid-series", children,
        dry_run=False, idx_start=0,
    ):
        events.append(ev)
    return svc, events


async def test_video_beats_bigger_container(monkeypatch):
    """The landed replacement survives; the rar retires to trash."""
    rar = _file("OAE-173.rar", "f-rar", int(4.6 * GB))
    mp4 = _file("OAE-173.1080p.mp4", "f-mp4", int(2.4 * GB))
    svc, _ = await _run([rar, mp4], monkeypatch)
    assert svc.trashed == ["f-rar"]
    assert ("f-mp4", "OAE-173.mp4") in svc.renamed


async def test_downgrade_replacement_keeps_both(monkeypatch):
    """A video below the credibility bar (25%) retires nothing."""
    iso = _file("SNIS-494.iso", "f-iso", int(23.8 * GB))
    avi = _file("SNIS-494.avi", "f-avi", 2 * GB)  # 8% — worse rip
    svc, events = await _run([iso, avi], monkeypatch)
    assert svc.trashed == []
    reasons = {e.get("reason") for e in events}
    assert "container_kept" in reasons


async def test_half_written_video_retires_nothing(monkeypatch):
    """A still-growing size must not condemn the container."""
    rar = _file("OAE-173.rar", "f-rar", 4 * GB)
    mp4 = _file("OAE-173.mp4", "f-mp4", int(3.9 * GB),
                phase="PHASE_TYPE_RUNNING")
    svc, _ = await _run([rar, mp4], monkeypatch)
    assert svc.trashed == []


async def test_container_only_group_untouched(monkeypatch):
    """No size contest between containers — volume sets stay whole."""
    p1 = _file("ABC-123.part1.rar", "f-p1", 4 * GB)
    p2 = _file("ABC-123.part2.rar", "f-p2", 3 * GB)
    svc, _ = await _run([p1, p2], monkeypatch)
    assert svc.trashed == []
    assert svc.renamed == []


async def test_two_videos_smaller_still_trashed(monkeypatch):
    """Regression: plain video-vs-video dedupe is unchanged."""
    big = _file("SONE-092.mp4", "f-big", 26 * GB)
    small = _file("SONE-092(1).mp4", "f-small", 8 * GB)
    svc, _ = await _run([big, small], monkeypatch)
    assert svc.trashed == ["f-small"]


async def test_code_named_junk_still_trashed(monkeypatch):
    """A code-named txt is not a container — dedupe takes it as before."""
    mp4 = _file("MIDV-001.mp4", "f-mp4", 2 * GB)
    txt = _file("MIDV-001.txt", "f-txt", 1 * MB)
    svc, _ = await _run([mp4, txt], monkeypatch)
    assert svc.trashed == ["f-txt"]


async def test_wrapper_folder_with_video_beats_container(monkeypatch):
    """A wrapper holding the film ranks as playable and wins the group;
    the smaller-payload container is a credible-replacement loser."""
    rar = _file("OAE-173.rar", "f-rar", int(4.6 * GB))
    wrap = _folder("oae-173", "d-wrap")
    graph = {"d-wrap": [_file("oae-173.mp4", "f-inner", int(2.4 * GB))]}
    svc, events = await _run([rar, wrap], monkeypatch, graph=graph)
    # The wrapper survives the dedupe (winner_folder path takes over);
    # the point of this test is only that the video-bearing wrapper is
    # never the trash-loser to a bare container.
    assert "d-wrap" not in svc.trashed
