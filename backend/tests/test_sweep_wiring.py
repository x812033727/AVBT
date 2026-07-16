"""The library-wide sweeps must actually be reached from _sweep_root_once.

Both series_junk and dup_copies clean leftovers nothing else owns, and
both are invoked by a local import inside a try/except that swallows
everything. If a rename or a cycle ever breaks that import, the sweep
goes quiet and the leftovers pile back up with no failing test and no
error in the log — which is exactly how the 111 ad clips and the 112
collision copies accumulated in the first place.
"""

import app.services.archiver as arch


async def _run_sweep(monkeypatch, calls):
    async def fake_junk(svc, *, dry_run):
        calls.append(("junk", dry_run))
        return {"trashed": 0}

    async def fake_dups(svc, *, dry_run):
        calls.append(("dups", dry_run))
        return {"trashed": 0, "renamed": 0}

    monkeypatch.setattr("app.services.series_junk.purge_series_junk", fake_junk)
    monkeypatch.setattr("app.services.dup_copies.sweep_dup_copies", fake_dups)
    # Everything the sweep would otherwise do against PikPak.
    _stub_pikpak(monkeypatch)
    return await arch._sweep_root_once(cleanup_all_targets=True)


def _stub_pikpak(monkeypatch):
    """Everything the sweep would otherwise do against PikPak.

    ``_all_tracked_target_parent_ids`` must return something: both sweeps
    live under ``if target_parent_ids:``, so an empty set skips them and
    every assertion here would pass without the code being reached.
    """
    async def no_migration(*a, **kw):
        return
        yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr("app.services.reorganize._phase1_migrate_root",
                        no_migration)
    monkeypatch.setattr(arch, "_all_tracked_target_parent_ids",
                        _async_returning({"a-target-folder"}))
    monkeypatch.setattr(arch, "_cleanup_target_parents", _async_returning(0))


def _async_returning(value):
    async def fn(*a, **kw):
        return value
    return fn


async def test_cleanup_all_reaches_both_sweeps(monkeypatch):
    calls: list[tuple[str, bool]] = []
    try:
        await _run_sweep(monkeypatch, calls)
    except Exception:  # noqa: BLE001 — the rest of the sweep isn't the point
        pass
    assert ("junk", False) in calls, "series junk sweep not reached"
    assert ("dups", False) in calls, "dup copies sweep not reached"


async def test_a_failing_sweep_does_not_take_the_pass_down(monkeypatch):
    # These run inside the archive loop; one raising must not stop the
    # moves and finalizes the loop exists for.
    async def boom(svc, *, dry_run):
        raise RuntimeError("pikpak down")

    monkeypatch.setattr("app.services.dup_copies.sweep_dup_copies", boom)
    monkeypatch.setattr("app.services.series_junk.purge_series_junk", boom)
    _stub_pikpak(monkeypatch)
    await arch._sweep_root_once(cleanup_all_targets=True)   # must not raise
