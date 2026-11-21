"""finalize with 2+ per-code candidate folders: a settled, completely
listed, zero-child shell must not veto the real one.

Live 2026-09-01: MADV-513's 3.67GB video sat in ``kpkp3.com-MADV513``
beside the empty ``aavv38.xyz@MADV513`` shell; run_finalize aborted every
pass on「2 個候選資料夾」so neither the flatten nor the shell reaper ever
ran, and the orphan reaper (files still on PikPak) could not close the
row either. Same shape: ORECO-824/825/828 (ad shell + empty shell),
IPZZ-958 / ORECO-817 (two empty shells beside a flattened CODE.mp4)."""

import app.services.archiver as arch
import app.services.finalize as fin
from app.services.finalize import finalize_code_folder_stream
from tests.test_finalize import FakeSvc, _file, _folder

SERIES = "AVBT/製作商/S/系列"


class _Svc(FakeSvc):
    """Per-folder settle control + optional partial listing."""

    def __init__(self, graph, path_ids=None, *, unsettled=(), partial=()):
        super().__init__(graph, path_ids)
        self._unsettled = set(unsettled)
        self._partial = set(partial)

    def move_settled(self, source_id):
        return source_id not in self._unsettled

    async def list_all_files(self, parent_id, *, cap=5000):
        kids, _p = await super().list_all_files(parent_id, cap=cap)
        return kids, parent_id in self._partial


def _wire(monkeypatch, hits):
    async def resolve(code):
        return f"{SERIES}/{code}"

    async def code_folders(svc, code):
        return hits

    monkeypatch.setattr(arch, "_resolve_archive_path_by_code", resolve)
    monkeypatch.setattr(fin, "presence_code_folders", code_folders)


async def _run(svc, code="MADV-513"):
    return [e async for e in finalize_code_folder_stream(
        svc, code, folder_id=None, dry_run=False)]


def _hits(*leaves):
    return [(leaf, leaf, f"{SERIES}/{leaf}") for leaf in leaves]


async def test_empty_settled_shell_is_ignored_and_real_folder_flattened(monkeypatch):
    video = _file("kpkp3.com-MADV513.mp4", "v", 3500)
    svc = _Svc({
        "series": [_folder("aavv38.xyz@MADV513", "shellA"),
                   _folder("kpkp3.com-MADV513", "wrapB")],
        "shellA": [],
        "wrapB": [video],
    }, {SERIES: "series"})
    _wire(monkeypatch, _hits("shellA", "wrapB"))

    events = await _run(svc)

    assert not [e for e in events if e["type"] == "error"], events
    assert svc.moved == [(["v"], "series")]
    assert ("v", "MADV-513.mp4") in svc.renamed
    # The evacuated wrapper is retired; the ignored shell is NOT touched
    # by this pass (it gets its own turn once it is the sole candidate).
    assert svc.trashed == ["wrapB"]
    assert "shellA" not in svc.trashed


async def test_all_empty_shells_take_first_and_retire_it_when_parent_has_video(monkeypatch):
    svc = _Svc({
        "series": [_folder("IPZZ-958-C", "shellA"),
                   _folder("hkbisi.com@IPZZ-958-C", "shellB"),
                   _file("IPZZ-958.mp4", "loose", 4000)],
        "shellA": [],
        "shellB": [],
    }, {SERIES: "series"})
    _wire(monkeypatch, _hits("shellA", "shellB"))

    events = await _run(svc, "IPZZ-958")

    assert not [e for e in events if e["type"] == "error"], events
    assert svc.trashed == ["shellA"]  # one shell per pass; B untouched
    assert not svc.moved and not svc.purged


async def test_unsettled_empty_candidate_still_aborts(monkeypatch):
    """A freshly moved wrapper lists optimistically empty (#140) — it is
    live, not a shell, so the ambiguity stands and nothing happens."""
    svc = _Svc({
        "series": [_folder("a", "shellA"), _folder("b", "wrapB")],
        "shellA": [],
        "wrapB": [_file("MADV-513.mp4", "v", 3500)],
    }, {SERIES: "series"}, unsettled={"shellA"})
    _wire(monkeypatch, _hits("shellA", "wrapB"))

    events = await _run(svc)

    errs = [e for e in events if e["type"] == "error"]
    assert errs and "2 個候選資料夾" in errs[0]["message"]
    assert not svc.moved and not svc.trashed and not svc.renamed


async def test_partial_listing_counts_as_live(monkeypatch):
    svc = _Svc({
        "series": [_folder("a", "shellA"), _folder("b", "wrapB")],
        "shellA": [],
        "wrapB": [_file("MADV-513.mp4", "v", 3500)],
    }, {SERIES: "series"}, partial={"shellA"})
    _wire(monkeypatch, _hits("shellA", "wrapB"))

    events = await _run(svc)

    errs = [e for e in events if e["type"] == "error"]
    assert errs and "2 個候選資料夾" in errs[0]["message"]
    assert not svc.moved and not svc.trashed


async def test_two_live_candidates_still_abort(monkeypatch):
    svc = _Svc({
        "series": [_folder("a", "wrapA"), _folder("b", "wrapB")],
        "wrapA": [_file("MADV-513.mp4", "v1", 2000)],
        "wrapB": [_file("MADV-513.mp4", "v2", 3500)],
    }, {SERIES: "series"})
    _wire(monkeypatch, _hits("wrapA", "wrapB"))

    events = await _run(svc)

    errs = [e for e in events if e["type"] == "error"]
    assert errs and "2 個候選資料夾" in errs[0]["message"]
    assert not svc.moved and not svc.trashed


async def test_listing_error_counts_as_live(monkeypatch):
    class Boom(_Svc):
        async def list_all_files(self, parent_id, *, cap=5000):
            if parent_id == "shellA":
                raise RuntimeError("pikpak down")
            return await super().list_all_files(parent_id, cap=cap)

    svc = Boom({
        "series": [_folder("a", "shellA"), _folder("b", "wrapB")],
        "shellA": [],
        "wrapB": [_file("MADV-513.mp4", "v", 3500)],
    }, {SERIES: "series"})
    _wire(monkeypatch, _hits("shellA", "wrapB"))

    events = await _run(svc)

    errs = [e for e in events if e["type"] == "error"]
    assert errs and "2 個候選資料夾" in errs[0]["message"]
    assert not svc.moved and not svc.trashed
