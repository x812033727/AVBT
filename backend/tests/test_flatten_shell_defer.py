"""The wrapper a flatten empties must eventually be collected.

The flatten path stamps ``record_move_source(child.id)`` for each video
it moves out and then tests ``move_settled(child.id)`` immediately, so
the gate is always shut on that pass and the emptied wrapper is left for
"a later pass". No later pass ever took it: ``_became_empty`` only
reports True for a folder emptied DURING the pass, so an already-empty
folder is invisible to it. These tests pin the hand-off that closes the
gap — and pin that it stays closed to in-flight download wrappers.
"""

import time
from types import SimpleNamespace

import app.services.archiver as arch
import app.services.pikpak as pk

MB = 1024 * 1024


class Svc(pk.PikPakService):
    """Just enough service to drive the defer/collect hand-off."""

    def __init__(self, children=None):
        super().__init__()
        self._graph = {k: list(v) for k, v in (children or {}).items()}
        self.trashed: list[list[str]] = []
        self._boot_guard_until = 0.0

    async def list_all_files(self, parent_id, *, cap=5000):
        return list(self._graph.get(parent_id, [])), False

    async def trash_files(self, ids):
        self.trashed.append(list(ids))
        for nid in ids:
            self._graph.pop(nid, None)
        return {}


def _settle(svc, folder_id):
    """Backdate the stamp so the gate reads open, as it would 30min on."""
    svc._move_sources[folder_id] -= pk.MOVE_SETTLE_SECONDS * 2


def test_deferred_shell_waits_for_the_settle_gate():
    svc = Svc({"shell": []})
    svc.record_move_source("shell")
    svc.defer_shell("shell")

    # Fresh stamp: gate shut, nothing due yet. This is the whole reason
    # the shell could not be trashed on the flatten pass itself.
    assert svc.take_due_shells() == []

    _settle(svc, "shell")
    assert svc.take_due_shells() == ["shell"]


def test_due_shell_is_popped_so_it_gets_exactly_one_attempt():
    svc = Svc({"shell": []})
    svc.record_move_source("shell")
    svc.defer_shell("shell")
    _settle(svc, "shell")

    assert svc.take_due_shells() == ["shell"]
    # Popped, not retried — a wrapper that turns out not to be empty
    # must not re-queue itself on every sweep forever.
    assert svc.take_due_shells() == []


async def test_collect_trashes_an_empty_shell_but_spares_one_with_junk():
    leftover = SimpleNamespace(
        name="cover.jpg", id="jpg1", kind="drive#file", size=1 * MB
    )
    svc = Svc({"empty": [], "junky": [leftover]})
    for fid in ("empty", "junky"):
        svc.record_move_source(fid)
        svc.defer_shell(fid)
        _settle(svc, fid)

    assert await svc._trash_if_empty("empty") is True
    # _trash_if_empty re-lists: a wrapper still holding something is not
    # a shell, and the deferral does not override that.
    assert await svc._trash_if_empty("junky") is False
    assert svc.trashed == [["empty"]]


def test_in_flight_wrapper_is_never_deferred():
    """#129: a download wrapper PikPak has not written into yet lists as
    zero children. It was never moved out of, so it carries no stamp —
    ``move_settled`` would return True for it and ``_trash_if_empty``
    would happily delete it, killing the download. It is safe only
    because nothing puts it in this set: membership requires that WE
    emptied the folder."""
    svc = Svc({"inflight": []})

    assert svc.move_settled("inflight") is True  # no stamp → reads open
    assert svc.take_due_shells() == []  # …but it was never deferred
    assert svc._deferred_shells == {}


async def test_sweep_drains_deferred_shells_with_nothing_moved(monkeypatch):
    """The drain must not hang off ``target_parent_ids``. A shell's own
    folder normally never receives anything again, so gating the drain
    on "this sweep moved something" would leave it uncollected — the
    live failure this fixes."""
    svc = Svc({"shell": []})
    svc.record_move_source("shell")
    svc.defer_shell("shell")
    _settle(svc, "shell")
    monkeypatch.setattr(arch, "pikpak_service", svc)

    async def _no_moves(*a, **kw):
        return
        yield  # pragma: no cover — makes this an async generator

    # _sweep_root_once imports this locally from .reorganize (cycle).
    import app.services.reorganize as reorg

    monkeypatch.setattr(reorg, "_phase1_migrate_root", _no_moves)
    monkeypatch.setattr(arch.settings, "pikpak_sweep_fallback_root", False)

    moved = await arch._sweep_root_once()

    assert moved == 0
    assert svc.trashed == [["shell"]]


def test_defer_shell_ignores_empty_id():
    svc = Svc()
    svc.defer_shell("")
    assert svc._deferred_shells == {}


def test_deferred_set_survives_only_in_memory():
    """Documented limitation: a restart drops the set and the shell
    leaks exactly as it does today. No stale id is ever acted on."""
    svc = Svc({"shell": []})
    svc.defer_shell("shell")
    assert svc._deferred_shells["shell"] <= time.time()

    fresh = Svc({"shell": []})
    assert fresh._deferred_shells == {}
