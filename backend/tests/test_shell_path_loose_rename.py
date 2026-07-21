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
    # A folder squatting on a marked part's canonical target — the
    # rename must divert through _uniquify_target, never land ON the
    # existing name. (A bare pair can't exercise this: require_marker
    # empties the plan and the assertion would be vacuous.)
    squatter = SimpleNamespace(
        id="d1", name="HUNTC-537_2.mp4", kind="drive#folder",
        size=None, phase="",
    )
    svc, renames = _svc([
        _file("489155.com@HUNTC-537-1.mp4", id_="f1"),
        _file("489155.com@HUNTC-537-2.mp4", id_="f2"),
        squatter,
    ])
    await _canonicalize_parent_code_videos(
        svc, "pid", "HUNTC-537", dry_run=False
    )
    assert ("f1", "HUNTC-537_1.mp4") in renames
    assert all(name != "HUNTC-537_2.mp4" for _fid, name in renames)
    assert any(fid == "f2" and name.startswith("HUNTC-537_2 (")
               for fid, name in renames)


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


async def test_shell_path_canonicalizes_before_success(monkeypatch):
    """Integration: driving the evacuated-shell verdict through
    ``finalize_code_folder_stream`` must land the loose-file rename —
    dropping the call site reseals BT names permanently and none of the
    helper-level tests above would notice."""
    import app.services.archiver as arch
    from app.services.finalize import finalize_code_folder_stream

    dirty = _file("489155.com@HUNTC-537.mp4", id_="loose")
    junk = _file("最新網址.txt", size=10, id_="txt")
    graph = {"shell": [junk], "series": [dirty]}
    path_ids = {"AVBT/製作商/S/系列/HUNTC-537": "shell",
                "AVBT/製作商/S/系列": "series"}
    renames: list[tuple[str, str]] = []

    class Svc:
        async def list_all_files(self, pid, *, cap=5000):
            return list(graph.get(pid, [])), False

        async def lookup_folder_id(self, path):
            return path_ids.get(path)

        async def rename_file(self, fid, name):
            renames.append((fid, name))
            return {}

        async def trash_files(self, ids):
            return {}

        async def delete_forever(self, ids):
            return {}

        async def move_files(self, ids, pid):
            return {}

        def record_move_source(self, sid):
            pass

        def move_settled(self, sid):
            return True

    async def fake_resolve(code):
        return "AVBT/製作商/S/系列/HUNTC-537"

    monkeypatch.setattr(arch, "_resolve_archive_path_by_code", fake_resolve)

    [e async for e in finalize_code_folder_stream(
        Svc(), "HUNTC-537", dry_run=False)]
    assert ("loose", "HUNTC-537.mp4") in renames
