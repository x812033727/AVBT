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
    # Ids ride along for playback lookups; non-videos and folders excluded.
    assert [e["id"] for e in s["video_files"]] == ["1", "2"]
    assert s["video_files"][0]["name"] == "ABC-123_1.mp4"


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


# ---------- files_for_code (playback lookups) ----------


async def test_files_for_code_folder_leaf(monkeypatch):
    fake = FakePikPak(
        tree={"fid": [f("v1", "ABC-123_1.mp4"), f("v2", "ABC-123_2.mp4"), f("j", "cover.jpg")]},
        paths={"AVBT/系列/某系列/ABC-123": "fid"},
    )
    monkeypatch.setattr(vc, "pikpak_service", fake)
    monkeypatch.setattr(
        vc, "presence_index", FakePresence({"ABC-123": ["AVBT/系列/某系列/ABC-123"]})
    )
    res = await vc.files_for_code("ABC-123")
    assert res["ok"] is True
    assert res["source"] == "presence"
    assert [x["id"] for x in res["files"]] == ["v1", "v2"]
    assert res["files"][0]["path"] == "AVBT/系列/某系列/ABC-123/ABC-123_1.mp4"


async def test_files_for_code_bare_file_leaf_resolved_via_parent(monkeypatch):
    fake = FakePikPak(
        tree={"done": [f("v9", "ABC-123.mp4"), f("x", "OTHER-1.mp4")]},
        paths={"AVBT/已完成": "done"},
    )
    monkeypatch.setattr(vc, "pikpak_service", fake)
    monkeypatch.setattr(
        vc, "presence_index", FakePresence({"ABC-123": ["AVBT/已完成/ABC-123.mp4"]})
    )
    res = await vc.files_for_code("ABC-123")
    assert res["ok"] is True
    assert [x["id"] for x in res["files"]] == ["v9"]
    assert res["files"][0]["path"] == "AVBT/已完成/ABC-123.mp4"


async def test_files_for_code_dedupes_across_copies(monkeypatch):
    fake = FakePikPak(
        tree={
            "a": [f("v1", "ABC-123.mp4")],
            "b": [f("v1", "ABC-123.mp4"), f("v2", "ABC-123_2.mp4")],
        },
        paths={"AVBT/已完成/ABC-123": "a", "AVBT/女優/某人/ABC-123": "b"},
    )
    monkeypatch.setattr(vc, "pikpak_service", fake)
    monkeypatch.setattr(
        vc,
        "presence_index",
        FakePresence({"ABC-123": ["AVBT/已完成/ABC-123", "AVBT/女優/某人/ABC-123"]}),
    )
    res = await vc.files_for_code("ABC-123")
    assert sorted(x["id"] for x in res["files"]) == ["v1", "v2"]


async def test_files_for_code_falls_back_to_task_folder(monkeypatch):
    fake = FakePikPak(tree={"tf": [f("v1", "ZZZ-999.mp4"), f("n", "note.txt")]})
    monkeypatch.setattr(vc, "pikpak_service", fake)
    monkeypatch.setattr(vc, "presence_index", FakePresence({}))

    async def latest(code):
        return "tf"

    monkeypatch.setattr(vc, "_latest_task_file_id", latest)
    res = await vc.files_for_code("ZZZ-999")
    assert res["ok"] is True
    assert res["source"] == "task"
    assert [x["id"] for x in res["files"]] == ["v1"]


async def test_files_for_code_falls_back_to_bare_task_file(monkeypatch):
    fake = FakePikPak(
        metas={"vid": {"id": "vid", "name": "ZZZ-999.mp4", "kind": "drive#file"}}
    )
    monkeypatch.setattr(vc, "pikpak_service", fake)
    monkeypatch.setattr(vc, "presence_index", FakePresence({}))

    async def latest(code):
        return "vid"

    monkeypatch.setattr(vc, "_latest_task_file_id", latest)
    res = await vc.files_for_code("ZZZ-999")
    assert res["ok"] is True
    assert res["files"][0]["id"] == "vid"


async def test_files_for_code_not_found_anywhere(monkeypatch):
    monkeypatch.setattr(vc, "pikpak_service", FakePikPak())
    monkeypatch.setattr(vc, "presence_index", FakePresence({}))

    async def no_task(code):
        return ""

    monkeypatch.setattr(vc, "_latest_task_file_id", no_task)
    res = await vc.files_for_code("ZZZ-999")
    assert res["ok"] is False


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


async def test_count_sums_same_folder_loose_parts(monkeypatch):
    """Flattened layout: CODE_1 + CODE_2 sitting in the same 系列 folder
    are PARTS of one work — count must be 2, not max(1, 1)."""
    monkeypatch.setattr(vc, "presence_index", FakePresence({
        "IDBD-939": [
            "AVBT/製作商/アイポケ/未分類/IDBD-939_2.mp4",
            "AVBT/製作商/アイポケ/未分類/IDBD-939_1.mp4",
        ],
    }))
    monkeypatch.setattr(vc, "pikpak_service", FakePikPak())
    r = await vc.count_for_code("IDBD-939")
    assert r["ok"] and r["video_count"] == 2
    assert r["video_names"] == ["IDBD-939_1.mp4", "IDBD-939_2.mp4"]


async def test_count_still_maxes_across_duplicate_homes(monkeypatch):
    """Copies in DIFFERENT folders remain duplicate homes → max, not sum."""
    monkeypatch.setattr(vc, "presence_index", FakePresence({
        "ABC-123": [
            "AVBT/製作商/X/系列A/ABC-123.mp4",
            "AVBT/已完成/ABC-123.mp4",
        ],
    }))
    monkeypatch.setattr(vc, "pikpak_service", FakePikPak())
    r = await vc.count_for_code("ABC-123")
    assert r["ok"] and r["video_count"] == 1
    assert len(r["entries"]) == 2


async def test_count_all_loose_parts_beyond_folder_cap(monkeypatch):
    """Scattered layout: a 6-part work is 6 presence paths. Loose-file
    paths cost no listing, so the folder cap must not truncate parts."""
    parent = "AVBT/製作商/SODクリエイト/未分類"
    monkeypatch.setattr(vc, "presence_index", FakePresence({
        "SDMU-845": [f"{parent}/SDMU-845_{i}.mp4" for i in range(1, 7)],
    }))
    monkeypatch.setattr(vc, "pikpak_service", FakePikPak())
    r = await vc.count_for_code("SDMU-845")
    assert r["ok"] and r["video_count"] == 6
    assert r["video_names"] == [f"SDMU-845_{i}.mp4" for i in range(1, 7)]


async def test_count_folder_homes_still_capped(monkeypatch):
    """Folder homes each cost a lookup+listing — the cap stays for those."""
    homes = [f"AVBT/已完成/copy{i}/ABC-123" for i in range(5)]
    fake = FakePikPak(
        tree={f"fid{i}": [f(f"v{i}", "ABC-123.mp4")] for i in range(5)},
        paths={home: f"fid{i}" for i, home in enumerate(homes)},
    )
    monkeypatch.setattr(vc, "pikpak_service", fake)
    monkeypatch.setattr(vc, "presence_index", FakePresence({"ABC-123": homes}))
    r = await vc.count_for_code("ABC-123")
    assert r["ok"] and r["video_count"] == 1
    assert len(r["entries"]) == vc._MAX_PATHS


class CountingPikPak(FakePikPak):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.list_calls = 0

    async def list_all_files(self, parent_id):
        self.list_calls += 1
        return await super().list_all_files(parent_id)


async def test_files_for_code_all_loose_parts_one_listing(monkeypatch):
    """files_for_code must return every part of a scattered work and
    list each parent folder once, not once per part."""
    parent = "AVBT/製作商/SODクリエイト/未分類"
    monkeypatch.setattr(vc, "presence_index", FakePresence({
        "SDMU-845": [f"{parent}/SDMU-845_{i}.mp4" for i in range(1, 7)],
    }))
    fake = CountingPikPak(
        tree={"series": [f(f"i{i}", f"SDMU-845_{i}.mp4") for i in range(1, 7)]},
        paths={parent: "series"},
    )
    monkeypatch.setattr(vc, "pikpak_service", fake)
    r = await vc.files_for_code("SDMU-845")
    assert r["ok"]
    assert [x["name"] for x in r["files"]] == [f"SDMU-845_{i}.mp4" for i in range(1, 7)]
    assert fake.list_calls == 1


async def test_files_for_code_sorted_by_name(monkeypatch):
    """Play list must read _1, _2 even when presence yields _2 first."""
    monkeypatch.setattr(vc, "presence_index", FakePresence({
        "IDBD-939": [
            "AVBT/製作商/アイポケ/未分類/IDBD-939_2.mp4",
            "AVBT/製作商/アイポケ/未分類/IDBD-939_1.mp4",
        ],
    }))
    fake = FakePikPak(
        tree={"series": [f("i1", "IDBD-939_1.mp4"), f("i2", "IDBD-939_2.mp4")]},
        paths={"AVBT/製作商/アイポケ/未分類": "series"},
    )
    monkeypatch.setattr(vc, "pikpak_service", fake)
    r = await vc.files_for_code("IDBD-939")
    assert r["ok"]
    assert [x["name"] for x in r["files"]] == ["IDBD-939_1.mp4", "IDBD-939_2.mp4"]
