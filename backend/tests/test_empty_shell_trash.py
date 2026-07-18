"""Empty-shell trash (the #207/#209 ad-shell fix's leftover shape).

A wrapper whose task died before PikPak wrote a single byte gets
migrated as a bare folder and then deadlocks: finalize reads no_video
and skips, the reaper won't abandon while a per-code folder exists, and
the folder's presence path hides the code from the missing scan (live
07-15 batch: EKDV-244 / DVDMS-047 / ATOM-304 …). The aged retry pass
may now trash a COMPLETELY empty tree — but only behind the move-settle
gate on the folder itself: a freshly-moved folder lists empty while its
files are still in flight (#140), and row age alone cannot rule that
out because the sweep can migrate a wrapper days after its row was
created.
"""

from types import SimpleNamespace

from app.services.finalize import finalize_code_folder_stream


def _file(name, fid, size=1):
    return SimpleNamespace(
        id=fid, name=name, kind="drive#file", size=size,
        parent_id=None, created_time=None, thumbnail_link=None,
        phase="PHASE_TYPE_COMPLETE", duration=None,
    )


def _folder(name, fid):
    f = _file(name, fid, 0)
    f.kind = "drive#folder"
    return f


class _GatelessSvc:
    """No ``move_settled`` at all — proves the inline path (opt-in off)
    never consults the gate."""

    def __init__(self, graph):
        self._graph = {k: list(v) for k, v in graph.items()}
        self.trashed: list[str] = []

    async def list_all_files(self, parent_id, *, cap=5000):
        return list(self._graph.get(parent_id, [])), False

    async def trash_files(self, ids):
        self.trashed.extend(ids)
        return {}

    async def lookup_folder_id(self, path):
        return None


class _Svc(_GatelessSvc):
    def __init__(self, graph, settled=True):
        super().__init__(graph)
        self._settled = settled
        self.gate_queries: list[str] = []

    def move_settled(self, folder_id):
        self.gate_queries.append(folder_id)
        return self._settled


async def _run(svc, code, folder_id, **kw):
    return [e async for e in finalize_code_folder_stream(
        svc, code, folder_id=folder_id, dry_run=False, **kw)]


async def test_empty_tree_trashed_when_settled():
    svc = _Svc({"codef": []})
    events = await _run(svc, "EKDV-244", "codef", allow_shell_trash=True)
    assert svc.trashed == ["codef"]
    assert any(e.get("action") == "trash"
               and e.get("reason") == "empty_shell_no_video"
               for e in events)
    done = next(e for e in events if e["type"] == "done")
    assert done["result"]["no_video"] is True
    assert done["result"]["trashed"] == 1


async def test_empty_subfolders_only_still_counts_as_empty():
    # Folders but zero files anywhere — still no data to lose.
    svc = _Svc({"codef": [_folder("Sub", "s1")], "s1": []})
    events = await _run(svc, "DVDMS-047", "codef", allow_shell_trash=True)
    assert svc.trashed == ["codef"]
    assert any(e.get("reason") == "empty_shell_no_video" for e in events)


async def test_empty_tree_skipped_while_gate_closed():
    # Freshly-moved wrapper (#140): listing is optimistic-empty, the
    # stamp holds the gate — nothing may be trashed.
    svc = _Svc({"codef": []}, settled=False)
    events = await _run(svc, "ATOM-304", "codef", allow_shell_trash=True)
    assert svc.trashed == []
    assert svc.gate_queries == ["codef"]
    assert any(e["type"] == "warn" and "略過" in e["message"] for e in events)
    done = next(e for e in events if e["type"] == "done")
    assert done["result"]["trashed"] == 0


async def test_inline_path_never_consults_gate():
    # allow_shell_trash=False (inline/sweep finalize): the old
    # skip-forever behaviour, and the gate must not even be queried —
    # this svc has no move_settled and would raise if it were.
    svc = _GatelessSvc({"codef": []})
    events = await _run(svc, "EKDV-244", "codef")
    assert svc.trashed == []
    assert any(e["type"] == "warn" and "略過" in e["message"] for e in events)


async def test_depth_truncated_empty_view_is_inconclusive():
    # Every visible level is folders-only but the walk hit MAX_DEPTH —
    # a file below the horizon is simply invisible. Never trash.
    svc = _Svc({
        "codef": [_folder("DVD", "d1")],
        "d1": [_folder("Disc1", "d2")],
        "d2": [_folder("VIDEO_TS", "d3")],
        "d3": [_file("VTS_01_1.VOB", "vob", 4_000_000_000)],
    })
    events = await _run(svc, "IESP-999", "codef", allow_shell_trash=True)
    assert svc.trashed == []
    assert any(e["type"] == "warn" and "略過" in e["message"] for e in events)
