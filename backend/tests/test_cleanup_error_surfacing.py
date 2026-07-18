"""``_cleanup_target_parents`` must surface per-file errors that phase-2
cleanup yields instead of silently discarding them. The consuming loop
used to do a bare ``async for _ev in ...: pass`` — a real "整理 PikPak
資料夾" failure (rename/trash/move exception on one file) vanished with
no log line and no counter, so a stuck folder looked identical to a
clean one from the outside."""

from types import SimpleNamespace

import app.services.archiver as arch
import app.services.reorganize as reorg


def _child(name="x.mp4", id_="f1"):
    return SimpleNamespace(id=id_, name=name, kind="drive#file", size=1)


async def test_phase2_errors_are_logged_and_counted(monkeypatch, caplog):
    async def fake_list_files(pid, size=500):
        return [_child()]

    async def fake_phase2(target_path, target_id, children, *, dry_run, idx_start):
        yield {"type": "progress", "action": "error", "target": None,
               "reason": "boom", "source": "x.mp4"}
        yield {"type": "progress", "action": "skip", "target": "y.mp4",
               "reason": "already_clean", "source": "y.mp4"}

    monkeypatch.setattr(
        arch, "pikpak_service", SimpleNamespace(list_files=fake_list_files),
    )
    monkeypatch.setattr(reorg, "_phase2_cleanup_target", fake_phase2)

    before = arch._cleanup_error_total
    with caplog.at_level("WARNING"):
        cleaned = await arch._cleanup_target_parents({"pid-1"})

    assert cleaned == 1
    assert arch._cleanup_error_total == before + 1
    assert any("phase-2 cleanup" in r.message for r in caplog.records)
    assert arch.state.to_dict()["cleanup_error_total"] == arch._cleanup_error_total


async def test_no_errors_means_no_warning_and_counter_unchanged(monkeypatch, caplog):
    async def fake_list_files(pid, size=500):
        return [_child()]

    async def fake_phase2(target_path, target_id, children, *, dry_run, idx_start):
        yield {"type": "progress", "action": "skip", "target": "y.mp4",
               "reason": "already_clean", "source": "y.mp4"}

    monkeypatch.setattr(
        arch, "pikpak_service", SimpleNamespace(list_files=fake_list_files),
    )
    monkeypatch.setattr(reorg, "_phase2_cleanup_target", fake_phase2)

    before = arch._cleanup_error_total
    with caplog.at_level("WARNING"):
        cleaned = await arch._cleanup_target_parents({"pid-1"})

    assert cleaned == 1
    assert arch._cleanup_error_total == before
    assert not any("phase-2 cleanup" in r.message for r in caplog.records)
