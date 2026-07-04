from types import SimpleNamespace

import app.services.video_count as vc


def f(id_, name, kind="drive#file"):
    return SimpleNamespace(id=id_, name=name, kind=kind)


def folder(id_, name):
    return f(id_, name, kind="drive#folder")


class FakePikPak:
    """Minimal stand-in for pikpak_service: a {parent_id: children} tree,
    {path: folder_id} lookup and {file_id: meta} store."""

    def __init__(self, tree=None, paths=None, metas=None):
        self.tree = tree or {}
        self.paths = paths or {}
        self.metas = metas or {}

    async def list_all_files(self, parent_id):
        return list(self.tree.get(parent_id, [])), False

    async def lookup_folder_id(self, path):
        return self.paths.get(path, "")

    async def file_meta(self, file_id):
        return self.metas.get(file_id, {})


class FakePresence:
    def __init__(self, paths_by_code):
        self._by_code = paths_by_code

    async def get(self, *, force=False):
        return set(self._by_code)

    def paths_for(self, code):
        return list(self._by_code.get(code, []))


def test_summarize_children_counts_videos_and_subfolders():
    s = vc.summarize_children(
        [
            f("1", "ABC-123_1.mp4"),
            f("2", "ABC-123_2.mkv"),
            f("3", "cover.jpg"),
            folder("d", "extras"),
        ]
    )
    assert s["video_count"] == 2
    assert s["total_files"] == 3
    assert s["video_names"] == ["ABC-123_1.mp4", "ABC-123_2.mkv"]
    assert s["subfolder_ids"] == ["d"]


async def test_count_for_file_id_folder_with_nested_level(monkeypatch):
    fake = FakePikPak(
        tree={
            "task": [f("1", "ABC-123_1.mp4"), folder("sub", "CD2")],
            "sub": [f("2", "ABC-123_2.mp4")],
        }
    )
    monkeypatch.setattr(vc, "pikpak_service", fake)
    res = await vc.count_for_file_id("task")
    assert res["ok"] is True
    assert res["video_count"] == 2
    assert res["source"] == "task"


async def test_count_for_file_id_single_file(monkeypatch):
    fake = FakePikPak(metas={"vid": {"id": "vid", "name": "ABC-123.mp4", "kind": "drive#file"}})
    monkeypatch.setattr(vc, "pikpak_service", fake)
    res = await vc.count_for_file_id("vid")
    assert res == {
        "ok": True,
        "video_count": 1,
        "video_names": ["ABC-123.mp4"],
        "source": "task",
    }


async def test_count_for_file_id_dangling(monkeypatch):
    fake = FakePikPak()
    monkeypatch.setattr(vc, "pikpak_service", fake)
    res = await vc.count_for_file_id("gone")
    assert res["ok"] is False


async def test_count_for_code_via_presence_folder(monkeypatch):
    fake = FakePikPak(
        tree={"fid": [f("1", "ABC-123_1.mp4"), f("2", "ABC-123_2.mp4"), f("3", "poster.jpg")]},
        paths={"AVBT/系列/某系列/ABC-123": "fid"},
    )
    monkeypatch.setattr(vc, "pikpak_service", fake)
    monkeypatch.setattr(
        vc, "presence_index", FakePresence({"ABC-123": ["AVBT/系列/某系列/ABC-123"]})
    )
    res = await vc.count_for_code("ABC-123")
    assert res["ok"] is True
    assert res["video_count"] == 2
    assert res["source"] == "presence"
    assert res["entries"] == [{"path": "AVBT/系列/某系列/ABC-123", "video_count": 2}]


async def test_count_for_code_bare_video_leaf(monkeypatch):
    monkeypatch.setattr(vc, "pikpak_service", FakePikPak())
    monkeypatch.setattr(
        vc, "presence_index", FakePresence({"ABC-123": ["AVBT/已完成/ABC-123.mp4"]})
    )
    res = await vc.count_for_code("ABC-123")
    assert res["ok"] is True
    assert res["video_count"] == 1


async def test_count_for_code_multiple_copies_takes_max(monkeypatch):
    fake = FakePikPak(
        tree={
            "a": [f("1", "ABC-123.mp4")],
            "b": [f("2", "ABC-123_1.mp4"), f("3", "ABC-123_2.mp4")],
        },
        paths={"AVBT/已完成/ABC-123": "a", "AVBT/女優/某人/ABC-123": "b"},
    )
    monkeypatch.setattr(vc, "pikpak_service", fake)
    monkeypatch.setattr(
        vc,
        "presence_index",
        FakePresence({"ABC-123": ["AVBT/已完成/ABC-123", "AVBT/女優/某人/ABC-123"]}),
    )
    res = await vc.count_for_code("ABC-123")
    # Duplicate homes of one work → max, not sum.
    assert res["video_count"] == 2
    assert len(res["entries"]) == 2


async def test_count_for_code_not_found_anywhere(monkeypatch):
    monkeypatch.setattr(vc, "pikpak_service", FakePikPak())
    monkeypatch.setattr(vc, "presence_index", FakePresence({}))

    async def no_task(code):
        return ""

    monkeypatch.setattr(vc, "_latest_task_file_id", no_task)
    res = await vc.count_for_code("ZZZ-999")
    assert res["ok"] is False


async def test_count_for_code_falls_back_to_task(monkeypatch):
    fake = FakePikPak(tree={"tf": [f("1", "ZZZ-999.mp4")]})
    monkeypatch.setattr(vc, "pikpak_service", fake)
    monkeypatch.setattr(vc, "presence_index", FakePresence({}))

    async def latest(code):
        return "tf"

    monkeypatch.setattr(vc, "_latest_task_file_id", latest)
    res = await vc.count_for_code("ZZZ-999")
    assert res["ok"] is True
    assert res["video_count"] == 1
    assert res["source"] == "task"


# ---------- pCloud (transfer-record based) ----------

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

import app.database as db  # noqa: E402
from app.models import PCloudTransfer  # noqa: E402


async def _seed_transfers(tmp_path, monkeypatch, rows):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(vc, "SessionLocal", maker)
    async with maker() as session:
        session.add_all(rows)
        await session.commit()
    return engine


async def test_count_for_code_pcloud_multi_part(tmp_path, monkeypatch):
    engine = await _seed_transfers(
        tmp_path,
        monkeypatch,
        [
            PCloudTransfer(
                pikpak_file_id="a", pikpak_name="ABC-123_1.mp4",
                pcloud_file_id=11, pcloud_folder_path="/From PikPak", status="done",
            ),
            PCloudTransfer(
                pikpak_file_id="b", pikpak_name="ABC-123_2.mp4",
                pcloud_file_id=12, pcloud_folder_path="/From PikPak", status="done",
            ),
            # Retried transfer of the same file → same pcloud_file_id, must dedupe.
            PCloudTransfer(
                pikpak_file_id="b", pikpak_name="ABC-123_2.mp4",
                pcloud_file_id=12, pcloud_folder_path="/From PikPak", status="done",
            ),
            # Different code sharing the label prefix — must not count.
            PCloudTransfer(
                pikpak_file_id="c", pikpak_name="ABC-999.mp4",
                pcloud_file_id=13, pcloud_folder_path="/From PikPak", status="done",
            ),
            # Failed transfer — must not count.
            PCloudTransfer(
                pikpak_file_id="d", pikpak_name="ABC-123_3.mp4",
                pcloud_file_id=14, pcloud_folder_path="/From PikPak", status="failed",
            ),
            # Non-video — must not count.
            PCloudTransfer(
                pikpak_file_id="e", pikpak_name="ABC-123.jpg",
                pcloud_file_id=15, pcloud_folder_path="/From PikPak", status="done",
            ),
        ],
    )
    res = await vc.count_for_code_pcloud("ABC-123")
    await engine.dispose()
    assert res["ok"] is True
    assert res["video_count"] == 2
    assert res["source"] == "transfer"
    assert res["entries"] == [{"path": "/From PikPak", "video_count": 2}]


async def test_count_for_code_pcloud_not_transferred(tmp_path, monkeypatch):
    engine = await _seed_transfers(tmp_path, monkeypatch, [])
    res = await vc.count_for_code_pcloud("ZZZ-999")
    await engine.dispose()
    assert res["ok"] is False
