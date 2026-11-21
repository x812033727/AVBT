"""phase-2 cleanup must name the files it acted on.

Live 2026-08-08 (r506): the flattened-stamp path trashed a duplicate
``ORER-010 (2).mp4`` and the only trace in the log was
``flattened stamp: phase-2 rename was pending, cleaned 1 folder(s)``.
Reconstructing *which* file disappeared cost two rounds of investigation
(presence index still listed the trashed path, live listing did not).
_cleanup_target_parents consumed the whole event stream and threw away
every successful action, keeping only errors — so a dedupe/rename done by
the sweep was indistinguishable from a no-op traversal.
"""

import logging
import types

import pytest

from app.services import archiver


def _ev(action, source, target, reason=None):
    return {
        "type": "progress",
        "action": action,
        "source": source,
        "target": target,
        "reason": reason,
    }


@pytest.fixture
def fake_pikpak(monkeypatch):
    async def list_files(pid, size=500):
        return [types.SimpleNamespace(id="f1", name="x", kind="drive#file")]

    monkeypatch.setattr(
        archiver, "pikpak_service",
        types.SimpleNamespace(list_files=list_files),
    )


def _patch_stream(monkeypatch, events):
    async def _stream(pid, target_id, children, dry_run, idx_start):
        for ev in events:
            yield ev

    from app.services import reorganize

    monkeypatch.setattr(reorganize, "_phase2_cleanup_target", _stream)


async def test_dedupe_names_the_trashed_file(
    monkeypatch, fake_pikpak, caplog
):
    _patch_stream(monkeypatch, [
        _ev("dedupe", "ORER-010 (2).mp4", "ORER-010.mp4", "較小重複版"),
    ])
    with caplog.at_level(logging.INFO, logger="app.services.archiver"):
        cleaned = await archiver._cleanup_target_parents({"pid1"})

    assert cleaned == 1
    text = caplog.text
    assert "ORER-010 (2).mp4" in text, "trashed file must be named"
    assert "dedupe" in text


async def test_rename_is_logged_with_old_and_new_name(
    monkeypatch, fake_pikpak, caplog
):
    _patch_stream(monkeypatch, [
        _ev("rename", "[Thz.la]abc-123.mp4", "ABC-123.mp4"),
    ])
    with caplog.at_level(logging.INFO, logger="app.services.archiver"):
        await archiver._cleanup_target_parents({"pid1"})

    assert "[Thz.la]abc-123.mp4" in caplog.text
    assert "ABC-123.mp4" in caplog.text


async def test_skip_only_traversal_stays_quiet(
    monkeypatch, fake_pikpak, caplog
):
    """The common case is a folder that needs nothing. Logging every
    untouched child would bury the real actions — keep it silent."""
    _patch_stream(monkeypatch, [
        _ev("skip", "ABC-123.mp4", "ABC-123.mp4", "already canonical"),
        _ev("skip", "ABC-124.mp4", "ABC-124.mp4", "already canonical"),
    ])
    with caplog.at_level(logging.INFO, logger="app.services.archiver"):
        cleaned = await archiver._cleanup_target_parents({"pid1"})

    assert cleaned == 1
    assert "phase-2 cleanup actions" not in caplog.text


async def test_errors_still_reported_and_counted(
    monkeypatch, fake_pikpak, caplog
):
    """The pre-existing error line must not regress."""
    _patch_stream(monkeypatch, [
        _ev("error", "bad.mp4", None, "boom"),
        _ev("dedupe", "dup.mp4", "keep.mp4", "smaller"),
    ])
    with caplog.at_level(logging.INFO, logger="app.services.archiver"):
        await archiver._cleanup_target_parents({"pid1"})

    assert "boom" in caplog.text
    assert "dup.mp4" in caplog.text


async def test_action_list_is_capped(monkeypatch, fake_pikpak, caplog):
    """A folder with hundreds of renames must not emit a megabyte line."""
    _patch_stream(monkeypatch, [
        _ev("rename", f"old{i}.mp4", f"NEW-{i}.mp4") for i in range(60)
    ])
    with caplog.at_level(logging.INFO, logger="app.services.archiver"):
        await archiver._cleanup_target_parents({"pid1"})

    line = [r for r in caplog.records if "phase-2 cleanup actions" in
            r.getMessage()]
    assert line, "capped line must still be emitted"
    msg = line[0].getMessage()
    assert "+40 more" in msg
    assert "old59.mp4" not in msg
