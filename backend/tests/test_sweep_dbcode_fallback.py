"""Phase-1 migrate must route landed wrappers by their OfflineTaskLog
code, not only by parsing the BT release name.

Live failures (2026-07-18, r72): ``OAE-155HD.mp4`` (glued HD suffix)
parses to nothing → skipped as ``no_code`` and stranded in AVBT/TASK for
hours; ``RCT-116-AVI@Touch99`` parses to the phantom code ``TOUCH-99`` →
parked in 已完成 under the wrong identity while finalize looped on
「找不到歸檔資料夾」. The DB row created at submit time knows the real
code either way.
"""

from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as db
import app.services.reorganize as reorg
from app.models import OfflineTaskLog


async def _maker(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True
    )
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(reorg, "SessionLocal", maker)
    return maker


def _row(
    file_id: str, code: str, btih: str, name: str = ""
) -> OfflineTaskLog:
    return OfflineTaskLog(
        code=code,
        magnet="magnet:?xt=urn:btih:" + btih * 40,
        task_id=f"task-{file_id or name}",
        file_id=file_id,
        name=name,
        phase="PHASE_TYPE_COMPLETE",
        archived=False,
    )


def _child(fid: str, name: str = "") -> SimpleNamespace:
    return SimpleNamespace(id=fid, name=name)


async def test_task_log_codes_maps_by_file_id(tmp_path, monkeypatch):
    maker = await _maker(tmp_path, monkeypatch)
    async with maker() as session:
        session.add_all(
            [
                _row("fid-1", "OAE-155", "A"),
                _row("fid-2", "RCT-116", "B"),
                OfflineTaskLog(  # code-less row must not shadow anything
                    code="",
                    magnet="magnet:?xt=urn:btih:" + "C" * 40,
                    file_id="fid-3",
                    phase="",
                    archived=False,
                ),
            ]
        )
        await session.commit()

    out = await reorg._task_log_codes(
        [_child("fid-1"), _child("fid-2"), _child("fid-3"), _child("")]
    )
    assert out == {"fid-1": "OAE-155", "fid-2": "RCT-116"}


async def test_task_log_codes_name_fallback_for_dead_letter_rows(
    tmp_path, monkeypatch
):
    # Live case r73: task died before landing → row has file_id='' and
    # the wrapper name (task title) parses to nothing. Exact name
    # equality must still route it; a file_id match must beat a name
    # match when both exist.
    maker = await _maker(tmp_path, monkeypatch)
    async with maker() as session:
        session.add_all(
            [
                _row("", "GDTM-148", "A", name="gdtm148hd"),
                _row("fid-x", "OAE-155", "B", name="shared-title"),
                _row("", "ZZZ-999", "C", name="shared-title"),
            ]
        )
        await session.commit()

    out = await reorg._task_log_codes(
        [
            _child("wrapper-1", "gdtm148hd"),
            _child("fid-x", "shared-title"),
            _child("wrapper-2", "no-row-for-this"),
        ]
    )
    # file_id wins for fid-x even though its name also matches a row.
    assert out == {"wrapper-1": "GDTM-148", "fid-x": "OAE-155"}


async def test_task_log_codes_empty_on_db_error(monkeypatch):
    class Boom:
        def __call__(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(reorg, "SessionLocal", Boom())
    assert await reorg._task_log_codes([_child("fid-1")]) == {}


class _FakePikPak:
    """Just enough of pikpak_service for _phase1_migrate_from."""

    def __init__(self, children):
        self.children = children
        self.moves: list[tuple[list[str], str]] = []
        self.renames: list[tuple[str, str]] = []

    async def folder_id(self, path):
        return "src-id" if path == "AVBT/TASK" else f"dst:{path}"

    async def list_files(self, parent_id, size=100):
        return self.children if parent_id == "src-id" else []

    async def move_files(self, ids, parent_id):
        self.moves.append((ids, parent_id))

    async def rename_file(self, file_id, name):
        self.renames.append((file_id, name))

    def record_move_source(self, file_id):
        pass


def _file(fid: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=fid, name=name, kind="drive#file", size=4_000_000_000
    )


async def _run_migrate(tmp_path, monkeypatch, children, rows):
    maker = await _maker(tmp_path, monkeypatch)
    async with maker() as session:
        session.add_all(rows)
        await session.commit()

    fake = _FakePikPak(children)
    monkeypatch.setattr(reorg, "pikpak_service", fake)

    async def resolve(code):
        return f"AVBT/製作商/S/未分類/{code}"

    monkeypatch.setattr(reorg, "_resolve_archive_path_by_code", resolve)

    events = []
    async for ev in reorg._phase1_migrate_from(
        "AVBT/TASK", dry_run=False, idx_start=0
    ):
        if ev.get("type") == "progress":
            events.append(ev)
    return fake, events


async def test_db_code_beats_unparseable_name(tmp_path, monkeypatch):
    fake, events = await _run_migrate(
        tmp_path,
        monkeypatch,
        [_file("fid-oae", "OAE-155HD.mp4")],
        [_row("fid-oae", "OAE-155", "A")],
    )
    (ev,) = events
    assert ev["action"] == "move"
    assert ev["target"] == "AVBT/製作商/S/未分類/OAE-155.mp4"
    assert fake.moves == [(["fid-oae"], "dst:AVBT/製作商/S/未分類")]
    assert ("fid-oae", "OAE-155.mp4") in fake.renames


async def test_db_code_beats_wrong_name_parse(tmp_path, monkeypatch):
    # Name alone parses to the phantom TOUCH-99; the row knows better.
    fake, events = await _run_migrate(
        tmp_path,
        monkeypatch,
        [_file("fid-rct", "RCT-116-AVI@Touch99.avi")],
        [_row("fid-rct", "RCT-116", "B")],
    )
    (ev,) = events
    assert ev["action"] == "move"
    assert ev["target"] == "AVBT/製作商/S/未分類/RCT-116.avi"


async def test_name_fallback_routes_dead_letter_wrapper(
    tmp_path, monkeypatch
):
    # Row abandoned before landing: file_id='' but the stored task title
    # equals the wrapper name. Name parsing yields nothing (glued
    # suffix); the name-keyed row must still route it.
    fake, events = await _run_migrate(
        tmp_path,
        monkeypatch,
        [_file("fid-new", "gdtm148hd.mp4")],
        [_row("", "GDTM-148", "A", name="gdtm148hd.mp4")],
    )
    (ev,) = events
    assert ev["action"] == "move"
    assert ev["target"] == "AVBT/製作商/S/未分類/GDTM-148.mp4"


async def test_no_row_keeps_existing_behaviour(tmp_path, monkeypatch):
    fake, events = await _run_migrate(
        tmp_path,
        monkeypatch,
        [
            _file("fid-abc", "ABC-123.mp4"),   # parseable → moves
            _file("fid-junk", "readme_no_code_here.txt"),  # → no_code skip
        ],
        [],
    )
    by_name = {ev["source"]: ev for ev in events}
    assert by_name["ABC-123.mp4"]["action"] == "move"
    assert by_name["readme_no_code_here.txt"]["action"] == "skip"
    assert by_name["readme_no_code_here.txt"]["reason"] == "no_code"
