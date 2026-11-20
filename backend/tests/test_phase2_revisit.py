"""Phase-2 cleanup must come back to a series folder it had to leave
half-done, and must be reached at all after a finalize flatten (#168).

Phase-2 only runs on folders the CURRENT sweep moved items into. When a
pass skips a wrapper/loser behind a time gate (settle grace, move-settle,
a file still transferring) nothing ever brings the folder back once the
gate opens — live: DVMM-107 / MADV-555 / MIH-004 kept ``CODE-SD.mp4`` +
``CODE_2.mp4`` for four weeks. Likewise a finalize flatten writes loose
files into the series folder outside any sweep's move accounting, so
that folder never sees phase-2 either.
"""

from types import SimpleNamespace

import pytest

import app.services.archiver as arch
import app.services.reorganize as reorg


@pytest.fixture(autouse=True)
def _isolate_queues():
    arch._revisit_parent_ids.clear()
    arch._reap_cleanup_paths.clear()
    yield
    arch._revisit_parent_ids.clear()
    arch._reap_cleanup_paths.clear()


def _child(name="x.mp4", id_="f1"):
    return SimpleNamespace(id=id_, name=name, kind="drive#file", size=1)


def _phase2_yielding(*events):
    async def fake_phase2(target_path, target_id, children, *, dry_run, idx_start):
        for ev in events:
            yield {"type": "progress", "source": "x", **ev}
    return fake_phase2


def _stub_cleanup_deps(monkeypatch, fake_phase2):
    async def fake_list_files(pid, size=500):
        return [_child()]
    monkeypatch.setattr(
        arch, "pikpak_service", SimpleNamespace(list_files=fake_list_files),
    )
    monkeypatch.setattr(reorg, "_phase2_cleanup_target", fake_phase2)


@pytest.mark.parametrize("reason", ["settling", "move_settling", "transferring"])
async def test_gated_skip_queues_folder_for_next_sweep(monkeypatch, reason):
    _stub_cleanup_deps(monkeypatch, _phase2_yielding(
        {"action": "skip", "target": "CODE", "reason": reason},
    ))
    await arch._cleanup_target_parents({"pid-1"})
    assert arch._take_due_revisits() == {"pid-1"}


async def test_clean_pass_retires_folder(monkeypatch):
    arch._revisit_parent_ids["pid-1"] = 5
    _stub_cleanup_deps(monkeypatch, _phase2_yielding(
        {"action": "skip", "target": "y.mp4", "reason": "already_clean"},
        {"action": "rename", "target": "CODE.mp4", "reason": None},
    ))
    await arch._cleanup_target_parents({"pid-1"})
    assert arch._take_due_revisits() == set()


async def test_non_gated_skips_do_not_arm_revisit(monkeypatch):
    # no_code / already_clean / container_kept / loser_folder_has_video
    # are terminal decisions, not "come back later".
    _stub_cleanup_deps(monkeypatch, _phase2_yielding(
        {"action": "skip", "target": None, "reason": "no_code"},
        {"action": "skip", "target": None, "reason": "loser_folder_has_video"},
        {"action": "skip", "target": None, "reason": "container_kept"},
    ))
    await arch._cleanup_target_parents({"pid-1"})
    assert arch._take_due_revisits() == set()


async def test_gated_pass_keeps_counting_down_existing_budget(monkeypatch):
    arch._revisit_parent_ids["pid-1"] = 2
    _stub_cleanup_deps(monkeypatch, _phase2_yielding(
        {"action": "skip", "target": "CODE", "reason": "move_settling"},
    ))
    await arch._cleanup_target_parents({"pid-1"})
    # Still gated → budget is NOT reset to a fresh _REVISIT_BUDGET.
    assert arch._revisit_parent_ids["pid-1"] == 2


def test_budget_exhausts_and_drops_folder():
    arch._revisit_parent_ids["pid-1"] = 1
    assert arch._take_due_revisits() == {"pid-1"}      # spends 1 → 0
    assert arch._take_due_revisits() == set()          # 0 → dropped
    assert "pid-1" not in arch._revisit_parent_ids


def test_registry_is_bounded():
    for i in range(arch._REVISIT_MAX):
        arch._note_revisit(f"pid-{i}", True)
    arch._note_revisit("one-too-many", True)
    assert "one-too-many" not in arch._revisit_parent_ids
    assert len(arch._revisit_parent_ids) == arch._REVISIT_MAX


async def test_sweep_drains_revisits_into_phase2(monkeypatch):
    """The wiring: a queued folder must actually reach
    ``_cleanup_target_parents`` on the next root sweep, even when that
    sweep moved nothing itself."""
    seen: list[set[str]] = []

    async def fake_cleanup(pids):
        seen.append(set(pids))
        return len(pids)

    async def no_migration(*a, **kw):
        return
        yield  # pragma: no cover — makes this an async generator

    async def fake_junk(svc, *, dry_run):
        return {"trashed": 0}

    async def fake_dups(svc, *, dry_run):
        return {"trashed": 0, "renamed": 0}

    monkeypatch.setattr("app.services.reorganize._phase1_migrate_root",
                        no_migration)
    monkeypatch.setattr("app.services.series_junk.purge_series_junk", fake_junk)
    monkeypatch.setattr("app.services.dup_copies.sweep_dup_copies", fake_dups)
    monkeypatch.setattr(arch, "_cleanup_target_parents", fake_cleanup)
    arch._revisit_parent_ids["pid-revisit"] = 3

    await arch._sweep_root_once(cleanup_all_targets=False)

    assert seen and "pid-revisit" in seen[0]
    assert arch._revisit_parent_ids["pid-revisit"] == 2


def test_queue_series_parents_skips_legacy_and_dedupes(monkeypatch):
    from app.services import pikpak_presence

    monkeypatch.setattr(arch.settings, "pikpak_archive_folder", "AVBT/已完成")
    monkeypatch.setattr(
        pikpak_presence.presence_index, "paths_for",
        lambda code: [
            "AVBT/製作商/本中/未分類/MIH-004_2.mp4",
            "AVBT/製作商/本中/未分類/MIH-004-SD.mp4",
            "AVBT/已完成/MIH-004.mp4",
            "MIH-004.mp4",
        ],
    )
    assert arch._queue_series_parents_for_cleanup("MIH-004") == 1
    assert arch._reap_cleanup_paths == {"AVBT/製作商/本中/未分類"}
    # Second call: already queued → nothing new.
    assert arch._queue_series_parents_for_cleanup("MIH-004") == 0


def test_queue_series_parents_respects_cap(monkeypatch):
    from app.services import pikpak_presence

    monkeypatch.setattr(
        pikpak_presence.presence_index, "paths_for",
        lambda code: ["AVBT/製作商/x/y/CODE.mp4"],
    )
    for i in range(arch._REAP_CLEANUP_PATHS_MAX):
        arch._reap_cleanup_paths.add(f"AVBT/製作商/x/p{i}")
    assert arch._queue_series_parents_for_cleanup("CODE") == 0
