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
