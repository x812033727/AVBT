"""``_resolve_folder_winner`` must keep every genuine part of a
multi-disc release and must not touch wrappers that are still
transferring. Regression for the live IDBD-939 incident where the old
single-winner rule trashed disc 1 as a "duplicate"."""

from types import SimpleNamespace

import app.services.reorganize as reorg

MB = 1024 * 1024


def _file(name, id, size_mb=600, phase="PHASE_TYPE_COMPLETE"):
    return SimpleNamespace(name=name, id=id, kind="drive#file",
                           size=size_mb * MB, phase=phase)


def _folder(name, id):
    return SimpleNamespace(name=name, id=id, kind="drive#folder",
                           size=None, phase="")


class StubSvc:
    def __init__(self, children):
        self._children = list(children)
        self.moved = []
        self.renamed = []
        self.trashed = []

    async def list_files(self, folder_id, size=200):
        return list(self._children)

    async def move_files(self, ids, parent_id):
        self.moved.append((list(ids), parent_id))
        return {}

    async def rename_file(self, fid, name):
        self.renamed.append((fid, name))
        return {}

    async def trash_files(self, ids):
        self.trashed.extend(ids)
        return {}


def _wrap():
    return SimpleNamespace(id="wrap", name="第一會所@idbd-939",
                           kind="drive#folder", size=None, phase="")


async def test_two_disc_boxset_keeps_both_as_parts(monkeypatch):
    svc = StubSvc([
        _file("idbd-939-1.mp4", "d1", 10300),
        _file("idbd-939-2.mp4", "d2", 10350),
        _file("cover.jpg", "j", 1),
        _file("下載說明.txt", "t", 0),
    ])
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "IDBD-939", "series", dry_run=False)
    assert result["action"] == "flatten"
    # Both discs moved out and named _1/_2; only real junk trashed.
    assert sorted(i for ids, _p in svc.moved for i in ids) == ["d1", "d2"]
    assert sorted(svc.renamed) == [("d1", "IDBD-939_1.mp4"),
                                   ("d2", "IDBD-939_2.mp4")]
    assert sorted(svc.trashed) == ["j", "t", "wrap"]


async def test_resolution_dup_still_drops_smaller(monkeypatch):
    svc = StubSvc([
        _file("MIDV-001.mp4", "big", 6000),
        _file("MIDV-001 (2).mp4", "small", 400),  # not all ≥500MB → dup
    ])
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "MIDV-001", "series", dry_run=False)
    assert result["action"] == "flatten"
    assert [i for ids, _p in svc.moved for i in ids] == ["big"]
    assert "small" in svc.trashed


async def test_single_video_names_after_code(monkeypatch):
    svc = StubSvc([_file("hhd800.com@MIDV-001.mp4", "v", 6000)])
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "MIDV-001", "series", dry_run=False)
    assert result["action"] == "flatten"
    assert svc.renamed == [("v", "MIDV-001.mp4")]


async def test_transferring_wrapper_is_skipped(monkeypatch):
    svc = StubSvc([
        _file("idbd-939-1.mp4", "d1", 10300),
        _file("idbd-939-2.mp4", "d2", 200, phase="PHASE_TYPE_RUNNING"),
    ])
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "IDBD-939", "series", dry_run=False)
    assert result["action"] == "skip"
    assert result["reason"] == "transferring"
    assert not svc.moved and not svc.renamed and not svc.trashed


async def test_dry_run_mutates_nothing(monkeypatch):
    svc = StubSvc([
        _file("idbd-939-1.mp4", "d1", 10300),
        _file("idbd-939-2.mp4", "d2", 10350),
    ])
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "IDBD-939", "series", dry_run=True)
    assert result["action"] == "flatten"
    assert not svc.moved and not svc.renamed and not svc.trashed
