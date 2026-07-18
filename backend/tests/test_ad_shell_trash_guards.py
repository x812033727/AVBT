"""Guards for the #207 ad-shell trash (adversarial-review fixes).

1. ``_list_subtree`` must flag a max-depth truncation as ``partial`` —
   otherwise a video nested deeper than MAX_DEPTH is invisible, the
   folder reads as "no video", and the ad-shell trash takes a real film.
2. The ad-shell trash must be opt-in (``allow_shell_trash``): only the
   aged retry/reap path may enable it. A freshly-moved folder can list
   its video subfolder as empty (#140 optimistic listings), so the
   inline/sweep finalize must keep the old skip behaviour.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.database as db
import app.services.archiver as arch
from app.models import OfflineTaskLog
from app.services.finalize import _list_subtree, finalize_code_folder_stream


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


class _Svc:
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

    # Aged retry path: the folder is long settled. The shell verdicts now
    # gate on this (2026-07-18 audit — an in-flight wrapper must not be
    # shell-trashed); these tests model the settled case.
    def move_settled(self, folder_id):
        return True


async def _run(svc, code, folder_id, **kw):
    return [e async for e in finalize_code_folder_stream(
        svc, code, folder_id=folder_id, dry_run=False, **kw)]


# ---------------------------------------------------------------------------
# fix 1 — depth truncation must surface as partial
# ---------------------------------------------------------------------------

async def test_list_subtree_flags_depth_truncation():
    # code folder → d1 → d2 → d3(folder at max depth; its video child is
    # never listed). Without the flag this walk claims completeness.
    svc = _Svc({
        "codef": [_folder("DVD", "d1")],
        "d1": [_folder("Disc1", "d2")],
        "d2": [_folder("VIDEO_TS", "d3")],
        "d3": [_file("VTS_01_1.VOB", "vob", 4_000_000_000)],
    })
    entries, _folders, partial, depth_truncated = await _list_subtree(
        svc, "codef"
    )
    assert partial is False                # page-cap semantics untouched
    assert depth_truncated is True         # truncation surfaced separately
    assert all(e.id != "vob" for e, _p in entries)  # the video was invisible


async def test_shallow_walk_is_not_partial():
    svc = _Svc({
        "codef": [_file("X-001.mp4", "v", 2048)],
    })
    _entries, _folders, partial, depth_truncated = await _list_subtree(
        svc, "codef"
    )
    assert partial is False
    assert depth_truncated is False


async def test_deep_nested_video_never_trashed_even_with_allow():
    # Depth-truncated folder + shallow junk: with the flag, the run aborts
    # on the partial listing before the ad-shell branch can trash.
    svc = _Svc({
        "codef": [
            _file("最新網址.txt", "txt", 1),
            _folder("DVD", "d1"),
        ],
        "d1": [_folder("Disc1", "d2")],
        "d2": [_folder("VIDEO_TS", "d3")],
        "d3": [_file("VTS_01_1.VOB", "vob", 4_000_000_000)],
    })
    events = await _run(svc, "X-001", "codef", allow_shell_trash=True)
    assert svc.trashed == []               # inconclusive inventory → skip
    assert any(e["type"] == "warn" and "略過" in e["message"] for e in events)


# ---------------------------------------------------------------------------
# fix 2 — ad-shell trash is opt-in
# ---------------------------------------------------------------------------

def _shell_graph():
    return {
        "codef": [
            _file("最新網址.txt", "txt", 1),
            _file("screen1.jpg", "jpg", 2),
        ],
    }


async def test_ad_shell_not_trashed_without_allow_flag():
    svc = _Svc(_shell_graph())
    events = await _run(svc, "X-001", "codef")
    assert svc.trashed == []
    assert any(e["type"] == "warn" and "略過" in e["message"] for e in events)
    done = [e for e in events if e["type"] == "done"][0]
    assert done["result"]["no_video"] is True
    assert done["result"]["trashed"] == 0


async def test_ad_shell_trashed_with_allow_flag():
    svc = _Svc(_shell_graph())
    events = await _run(svc, "X-001", "codef", allow_shell_trash=True)
    assert svc.trashed == ["codef"]
    assert any(e.get("reason") == "ad_shell_no_video" for e in events)
    done = [e for e in events if e["type"] == "done"][0]
    assert done["result"]["trashed"] == 1


async def test_small_container_does_not_block_shell_trash():
    # DVDMS-047 live shape: ads + a 29MB QQ-ad .rar. A sub-JUNK_BYTES
    # container is junk, not "container-swap's job" — without this the
    # row deadlocks (finalize skips forever, reaper sees an archived
    # copy, missing-scan reads the code as collected).
    svc = _Svc({"codef": [
        _file("screen1.jpg", "jpg", 2),
        _file("QQ真人祼聊免費試看.rar", "rar", 29 * 1024 * 1024),
    ]})
    events = await _run(svc, "X-001", "codef", allow_shell_trash=True)
    assert svc.trashed == ["codef"]
    assert any(e.get("reason") == "ad_shell_no_video" for e in events)


async def test_big_container_still_defers_to_container_swap():
    svc = _Svc({"codef": [
        _file("screen1.jpg", "jpg", 2),
        _file("X-001.iso", "iso", 8_000_000_000),
    ]})
    events = await _run(svc, "X-001", "codef", allow_shell_trash=True)
    assert svc.trashed == []
    assert any(e["type"] == "warn" and "略過" in e["message"] for e in events)


# ---------------------------------------------------------------------------
# call site — retry pass opens the gate only for aged rows
# ---------------------------------------------------------------------------

async def test_retry_pass_gates_allow_by_row_age(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(arch, "SessionLocal", m)
    monkeypatch.setattr(arch, "_finalize_attempts", {})
    now = datetime.utcnow()
    async with m() as s:
        for code, age_h in (("OLD-001", 26), ("NEW-001", 1)):
            s.add(OfflineTaskLog(
                code=code, magnet="m", btih="", task_id="", file_id="",
                name="", phase="", message="", archived=False,
                finalized=False, created_at=now - timedelta(hours=age_h),
            ))
        await s.commit()

    async def no_active():
        return set()

    async def _noop_refresh(codes, **kw):
        return 0

    monkeypatch.setattr(arch, "_active_task_ids", no_active)
    monkeypatch.setattr(
        "app.services.pikpak_presence.presence_index.refresh_codes",
        _noop_refresh,
    )
    calls: dict[str, bool] = {}

    async def fake_finalize(svc, code, *, folder_id=None,
                            allow_shell_trash=False):
        calls[code] = allow_shell_trash
        return True

    monkeypatch.setattr("app.services.finalize.run_finalize", fake_finalize)
    await arch._finalize_retry_pass()
    assert calls.get("OLD-001") is True    # aged past _ABANDON_GRACE
    assert calls.get("NEW-001") is False   # fresh row keeps the safe skip
    await engine.dispose()
