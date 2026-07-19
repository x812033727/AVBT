"""The evacuated-shell finalize path must canonicalize the code's loose
series-folder videos BEFORE succeeding — success stamps the row
finalized and nothing ever revisits the name (live 2026-07-19: seven
HUNTC codes sealed as ``489155.com@HUNTC-nnn.mp4``)."""

from types import SimpleNamespace

from app.services.finalize import _canonicalize_parent_code_videos

GB = 1024 ** 3


def _file(name, size=9 * GB, id_="f1", phase="PHASE_TYPE_COMPLETE"):
    return SimpleNamespace(
        id=id_, name=name, kind="drive#file", size=size, phase=phase,
    )


def _svc(kids, partial=False):
    renames: list[tuple[str, str]] = []

    async def list_all_files(pid):
        return kids, partial

    async def rename_file(fid, name):
        renames.append((fid, name))
        return {"id": fid, "name": name}

    svc = SimpleNamespace(
        list_all_files=list_all_files, rename_file=rename_file,
    )
    return svc, renames


async def test_dirty_singleton_renamed_to_canonical():
    svc, renames = _svc([_file("489155.com@HUNTC-537.mp4")])
    n = await _canonicalize_parent_code_videos(
        svc, "pid", "HUNTC-537", dry_run=False
    )
    assert n == 1
    assert renames == [("f1", "HUNTC-537.mp4")]


async def test_other_codes_untouched():
    svc, renames = _svc([
        _file("489155.com@HUNTC-537.mp4", id_="f1"),
        _file("hhd800.com@200GANA-3107.mp4", id_="f2"),
    ])
    await _canonicalize_parent_code_videos(
        svc, "pid", "HUNTC-537", dry_run=False
    )
    assert [r[0] for r in renames] == ["f1"]


async def test_transferring_file_defers_everything():
    svc, renames = _svc([
        _file("489155.com@HUNTC-537.mp4", phase="PHASE_TYPE_RUNNING"),
    ])
    n = await _canonicalize_parent_code_videos(
        svc, "pid", "HUNTC-537", dry_run=False
    )
    assert n == 0 and renames == []


async def test_partial_listing_defers_everything():
    svc, renames = _svc([_file("489155.com@HUNTC-537.mp4")], partial=True)
    n = await _canonicalize_parent_code_videos(
        svc, "pid", "HUNTC-537", dry_run=False
    )
    assert n == 0 and renames == []


async def test_collision_goes_through_uniquify():
    svc, renames = _svc([
        _file("489155.com@HUNTC-537.mp4", id_="f1"),
        _file("HUNTC-537.mp4", id_="f2"),
    ])
    await _canonicalize_parent_code_videos(
        svc, "pid", "HUNTC-537", dry_run=False
    )
    # f1's canonical target collides with the existing f2 — it must not
    # overwrite it. (The bare pair is a copy situation the dedup owns;
    # here we only assert no rename lands ON an existing name.)
    assert all(name != "HUNTC-537.mp4" for _fid, name in renames)


async def test_dry_run_counts_but_does_not_rename():
    svc, renames = _svc([_file("489155.com@HUNTC-537.mp4")])
    n = await _canonicalize_parent_code_videos(
        svc, "pid", "HUNTC-537", dry_run=True
    )
    assert n == 1 and renames == []


async def test_already_canonical_is_noop():
    svc, renames = _svc([_file("HUNTC-537.mp4")])
    n = await _canonicalize_parent_code_videos(
        svc, "pid", "HUNTC-537", dry_run=False
    )
    assert n == 0 and renames == []


async def test_listing_failure_never_raises():
    async def boom(pid):
        raise RuntimeError("pikpak down")

    svc = SimpleNamespace(list_all_files=boom)
    n = await _canonicalize_parent_code_videos(
        svc, "pid", "HUNTC-537", dry_run=False
    )
    assert n == 0


async def test_bare_pair_in_series_folder_not_renamed_as_discs():
    # 系列夾裡 bare + bare 同 canonical = 兩份完整片(dedup 的事),
    # require_marker=True 之下不得被編成 _1/_2。
    svc, renames = _svc([
        _file("HUNTC-537.mp4", id_="f1"),
        _file("HUNTC-537 (2).mp4", id_="f2"),
    ])
    n = await _canonicalize_parent_code_videos(
        svc, "pid", "HUNTC-537", dry_run=False
    )
    assert n == 0 and renames == []
