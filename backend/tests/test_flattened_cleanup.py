"""_already_flattened must not stamp a row while the loose file still
carries a BT-noise name — the sweep's in-memory phase-2 rename queue is
lost on restart, and the stamp made the dirty name permanent (live case
2026-07-15: 56 files / ~40 codes after a deploy restart). The guard
reruns phase-2 cleanup on the parent folder before stamping."""

from types import SimpleNamespace

import app.services.archiver as arch


def _file(name, size=2 * 1024 ** 3, id_="f1"):
    return SimpleNamespace(
        id=id_, name=name, kind="drive#file", size=size, parent_id="p1",
    )


async def test_cleanup_triggered_for_dirty_loose_name(monkeypatch):
    cleaned: list[set] = []

    async def fake_lookup(path):
        return "pid-series" if path == "AVBT/製作商/S/系列" else None

    async def fake_list_files(pid, size=500):
        return [_file("dccdom.com@200GANA-3146.mp4")]

    async def fake_cleanup(pids):
        cleaned.append(pids)
        return len(pids)

    monkeypatch.setattr(
        arch, "pikpak_service",
        SimpleNamespace(lookup_folder_id=fake_lookup,
                        list_files=fake_list_files),
    )
    monkeypatch.setattr(arch, "_cleanup_target_parents", fake_cleanup)

    await arch._cleanup_loose_parents_if_dirty([
        {"id": "f1", "name": "dccdom.com@200GANA-3146.mp4",
         "path": "AVBT/製作商/S/系列/dccdom.com@200GANA-3146.mp4"},
    ])
    assert cleaned == [{"pid-series"}]


async def test_cleanup_skipped_for_canonical_name(monkeypatch):
    cleaned: list[set] = []

    async def fake_lookup(path):
        return "pid-series"

    async def fake_list_files(pid, size=500):
        return [_file("GANA-3146.mp4")]

    async def fake_cleanup(pids):
        cleaned.append(pids)
        return len(pids)

    monkeypatch.setattr(
        arch, "pikpak_service",
        SimpleNamespace(lookup_folder_id=fake_lookup,
                        list_files=fake_list_files),
    )
    monkeypatch.setattr(arch, "_cleanup_target_parents", fake_cleanup)

    await arch._cleanup_loose_parents_if_dirty([
        {"id": "f1", "name": "GANA-3146.mp4",
         "path": "AVBT/製作商/S/系列/GANA-3146.mp4"},
    ])
    assert cleaned == []


async def test_cleanup_failure_never_raises(monkeypatch):
    async def boom(path):
        raise RuntimeError("pikpak down")

    monkeypatch.setattr(
        arch, "pikpak_service", SimpleNamespace(lookup_folder_id=boom),
    )
    await arch._cleanup_loose_parents_if_dirty([
        {"id": "f1", "name": "x.mp4", "path": "AVBT/a/x.mp4"},
    ])


async def _flattened_with_result(monkeypatch, result):
    """Drive _already_flattened to its files_for_code verdict."""
    import app.services.finalize as fin
    import app.services.video_count as vc

    async def no_path(code):
        return f"AVBT/製作商/S/系列/{code}"

    async def no_folder(path):
        return None

    async def no_code_folders(svc, code):
        return []

    async def fake_files_for_code(code):
        return result

    monkeypatch.setattr(arch, "_resolve_archive_path_by_code", no_path)
    monkeypatch.setattr(
        arch, "pikpak_service", SimpleNamespace(lookup_folder_id=no_folder),
    )
    monkeypatch.setattr(fin, "presence_code_folders", no_code_folders)
    monkeypatch.setattr(vc, "files_for_code", fake_files_for_code)

    async def no_cleanup(files):
        return None

    monkeypatch.setattr(arch, "_cleanup_loose_parents_if_dirty", no_cleanup)
    return await arch._already_flattened("OYCVR-058")


async def test_task_source_listing_is_not_flattened(monkeypatch):
    """presence knows nothing and files_for_code fell back to the task's
    own listing — the files are still in the download wrapper, so the
    row must NOT be stamped (live: OYCVR-058, closed with three
    fbfb.me@….partN.mp4 files never archived)."""
    assert await _flattened_with_result(monkeypatch, {
        "ok": True,
        "files": [{"id": "f1", "name": "fbfb.me@oycvr00058.part1.mp4",
                   "path": "fbfb.me@oycvr00058.part1.mp4"}],
        "source": "task",
    }) is False


async def test_presence_source_listing_is_flattened(monkeypatch):
    assert await _flattened_with_result(monkeypatch, {
        "ok": True,
        "files": [{"id": "f1", "name": "OYCVR-058_1.mp4",
                   "path": "AVBT/製作商/S/系列/OYCVR-058_1.mp4"}],
        "source": "presence",
    }) is True


async def test_wrapper_nested_presence_path_is_not_flattened(monkeypatch):
    """presence indexed the video INSIDE a wrapper folder sitting in the
    series folder — the sweep never flattened it, so the row must keep
    retrying instead of being stamped (live: EKDV-039, stamped while
    EKDV039.avi sat in (TVBOXNOW)_EKDV_038+039 six levels deep)."""
    assert await _flattened_with_result(monkeypatch, {
        "ok": True,
        "files": [{
            "id": "f1", "name": "EKDV039.avi",
            "path": "AVBT/製作商/クリスタル映像/INSTANTLOVE/"
                    "(TVBOXNOW)_EKDV_038+039/EKDV039.avi",
        }],
        "source": "presence",
    }) is False


async def test_mixed_depths_with_one_flattened_copy_still_stamps(monkeypatch):
    """A loose copy at the flattened depth qualifies even when a deeper
    wrapper duplicate lingers — the junk sweep owns the leftover."""
    assert await _flattened_with_result(monkeypatch, {
        "ok": True,
        "files": [
            {"id": "f1", "name": "OYCVR-058.mp4",
             "path": "AVBT/其他製作商/S/系列/OYCVR-058.mp4"},
            {"id": "f2", "name": "oycvr058.mp4",
             "path": "AVBT/其他製作商/S/系列/wrap/oycvr058.mp4"},
        ],
        "source": "presence",
    }) is True
