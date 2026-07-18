"""Multi-volume archives (.r00/.z01/.001): each volume is small but the
SET is the work. Volumes are neither video nor CONTAINER_EXTS today, so
finalize permanently purged them and the ad-shell verdict (per-file
JUNK_BYTES floor) read a volume set as junk (#219 review latent gap).
Volumes are container-family: trash-only, and the ad-shell content test
sums the set (None → assume legit)."""

from types import SimpleNamespace

from app.services.finalize import build_finalize_plan, wrapper_is_ad_shell
from app.services.jav_code import is_archive_volume


def _f(name, fid, size):
    return SimpleNamespace(
        id=fid, name=name, kind="drive#file", size=size, parent_id=None,
        created_time=None, thumbnail_link=None,
        phase="PHASE_TYPE_COMPLETE", duration=None,
    )


def _folder(name, fid):
    f = _f(name, fid, 0)
    f.kind = "drive#folder"
    return f


def test_is_archive_volume():
    assert is_archive_volume("ABC-001.r00")
    assert is_archive_volume("ABC-001.z01")
    assert is_archive_volume("ABC-001.001")
    assert not is_archive_volume("ABC-001.mp4")
    assert not is_archive_volume("ABC-001.rar")   # plain .rar is CONTAINER_EXTS


def test_volumes_are_trashed_not_purged():
    vid = _f("ABC-001.mp4", "v", 2_000_000_000)
    vol = _f("ABC-001.r00", "r0", 150_000_000)
    plan = build_finalize_plan("ABC-001", [(vid, "root"), (vol, "root")], "root")
    assert all(f.id != "r0" for f in plan.purge_files)   # never delete_forever
    assert any(f.id == "r0" for f in plan.trash_files)


class _Svc:
    def __init__(self, graph):
        self._graph = {k: list(v) for k, v in graph.items()}

    async def list_all_files(self, parent_id, *, cap=5000):
        return list(self._graph.get(parent_id, [])), False


async def test_volume_set_blocks_ad_shell_when_sum_substantial():
    # 3×150MB volumes = 450MB set ≥ JUNK_BYTES → content, not a shell.
    svc = _Svc({"w": [
        _f("X.r00", "a", 150_000_000),
        _f("X.r01", "b", 150_000_000),
        _f("X.r02", "c", 150_000_000),
        _f("ad.txt", "t", 1),
    ]})
    assert await wrapper_is_ad_shell(svc, "w") is False


async def test_single_tiny_volume_still_shell():
    svc = _Svc({"w": [_f("X.r00", "a", 20_000_000), _f("ad.txt", "t", 1)]})
    assert await wrapper_is_ad_shell(svc, "w") is True


async def test_null_size_volume_counts_as_content():
    svc = _Svc({"w": [_f("X.r00", "a", None), _f("ad.txt", "t", 1)]})
    assert await wrapper_is_ad_shell(svc, "w") is False
