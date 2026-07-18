"""Volume-family gaps found by the 2026-07-18 integration audit.

#227 made .r00/.z01/.001 volumes container-family in build_finalize_plan
and gave the ad-shell verdict a set-sum guard — but three sibling
deleters were left behind:

- the evacuated-shell re-plan (finalize.py, #213-era) still splits
  leftovers by raw CONTAINER_EXTS, so volume pieces are delete_forever'd;
- ``.partN.rar`` sets match neither the volume regex nor (post-#219) the
  per-piece size floor, so a scene-split film reads as an ad shell;
- series_junk trashes loose volume pieces on sight and retires
  ``.partN.rar`` pieces against a single piece's size.

The fix: ``is_archive_volume`` recognises ``.partN.rar`` families, the
re-plan uses the same container-family predicate as build_finalize_plan,
and series_junk treats volume pieces as keep-always (never auto-junk,
never per-piece retirement — a surviving lone piece is unextractable).
"""

from types import SimpleNamespace

import app.services.archiver as arch
from app.services.finalize import wrapper_is_ad_shell
from app.services.jav_code import is_archive_volume
from app.services.series_junk import is_series_junk


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


def test_part_rar_is_archive_volume():
    assert is_archive_volume("MIDV-001.part1.rar")
    assert is_archive_volume("MIDV-001.part12.rar")
    assert not is_archive_volume("MIDV-001.rar")   # plain .rar stays container
    assert not is_archive_volume("MIDV-001.mp4")


class _Svc:
    def __init__(self, graph):
        self._graph = {k: list(v) for k, v in graph.items()}

    async def list_all_files(self, parent_id, *, cap=5000):
        return list(self._graph.get(parent_id, [])), False


async def test_part_rar_set_blocks_ad_shell_when_sum_substantial():
    # 3×150MB .partN.rar = 450MB set ≥ JUNK_BYTES → content, not a shell.
    svc = _Svc({"w": [
        _f("X.part1.rar", "a", 150_000_000),
        _f("X.part2.rar", "b", 150_000_000),
        _f("X.part3.rar", "c", 150_000_000),
        _f("ad.txt", "t", 1),
    ]})
    assert await wrapper_is_ad_shell(svc, "w") is False


async def test_single_tiny_part_rar_still_shell():
    # One 20MB .part1.rar alone is an ad archive, same as a tiny .r00.
    svc = _Svc({"w": [_f("X.part1.rar", "a", 20_000_000)]})
    assert await wrapper_is_ad_shell(svc, "w") is True


def test_series_junk_never_condemns_volume_pieces():
    # Loose volume pieces in a 系列 folder: the SET is the work; trashing
    # pieces on sight (or retiring them one-by-one against a replacement
    # video) leaves an unextractable remainder. Keep-always.
    assert is_series_junk("MIDV-001.r00", 50_000_000) is False
    assert is_series_junk("MIDV-001.z01", 50_000_000) is False
    assert is_series_junk("MIDV-001.001", 50_000_000) is False
    assert is_series_junk(
        "MIDV-001.part2.rar", 200_000_000, video_bytes=5_000_000_000
    ) is False
    # Non-volume behaviour unchanged: bare junk still junk, whole
    # containers still retire against a credible replacement.
    assert is_series_junk("最新網址.txt", 10) is True
    assert is_series_junk(
        "MIDV-001.iso", 4_000_000_000, video_bytes=2_000_000_000
    ) is True


async def test_evacuated_shell_replan_trashes_volumes_not_purges(monkeypatch):
    """Re-plan branch: video confirmed at the parent, wrapper holds only
    a volume set + junk. Volumes must ride with containers → trash;
    only true junk is delete_forever'd."""
    from tests.test_finalize import FakeSvc, _collect

    path = "AVBT/系列/Foo/MIDV-001"
    svc = FakeSvc({
        "series": [
            _folder("MIDV-001", "codef"),
            _f("MIDV-001 rip.mp4", "bigvid", 2_000_000_000),
        ],
        "codef": [
            _f("MIDV-001.r00", "r0", 150_000_000),
            _f("MIDV-001.r01", "r1", 150_000_000),
            _f("最新網址.txt", "txt", 0),
        ],
    }, path_ids={path: "codef", "AVBT/系列/Foo": "series"})

    async def fake_resolve(code):
        return path

    monkeypatch.setattr(arch, "_resolve_archive_path_by_code", fake_resolve)
    events = await _collect(svc, "MIDV-001", "codef", dry_run=False)
    assert events[-1]["type"] == "done"
    assert "r0" not in svc.purged and "r1" not in svc.purged
    assert {"r0", "r1"}.issubset(set(svc.trashed))
    assert "txt" in svc.purged
