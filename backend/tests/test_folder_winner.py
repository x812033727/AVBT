"""``_resolve_folder_winner`` must keep every genuine part of a
multi-disc release and must not touch wrappers that are still
transferring. Regression for the live IDBD-939 incident where the old
single-winner rule trashed disc 1 as a "duplicate"."""

from datetime import UTC
from types import SimpleNamespace

import app.services.reorganize as reorg

MB = 1024 * 1024


def _file(name, id, size_mb=600, phase="PHASE_TYPE_COMPLETE"):
    return SimpleNamespace(name=name, id=id, kind="drive#file",
                           size=size_mb * MB, phase=phase)


def _folder(name, id):
    return SimpleNamespace(name=name, id=id, kind="drive#folder",
                           size=None, phase="")


class StubSvc:
    def __init__(self, children, dest_children=()):
        self._children = list(children)
        self._dest = list(dest_children)
        self.moved = []
        self.renamed = []
        self.trashed = []

    async def list_files(self, folder_id, size=200):
        if folder_id == "series":
            return list(self._dest)
        return list(self._children)

    async def move_files(self, ids, parent_id):
        self.moved.append((list(ids), parent_id))
        return {}

    async def rename_file(self, fid, name):
        self.renamed.append((fid, name))
        return {}

    async def trash_files(self, ids):
        self.trashed.extend(ids)
        return {}

    # Settle gate: stub moves are instantaneous, so tests default to an
    # open gate; set ``settled = False`` to simulate an in-flight move.
    settled = True

    def record_move_source(self, source_id):
        pass

    def move_settled(self, source_id):
        return self.settled


def _wrap():
    return SimpleNamespace(id="wrap", name="第一會所@idbd-939",
                           kind="drive#folder", size=None, phase="")


async def test_two_disc_boxset_keeps_both_as_parts(monkeypatch):
    svc = StubSvc([
        _file("idbd-939-1.mp4", "d1", 10300),
        _file("idbd-939-2.mp4", "d2", 10350),
        _file("cover.jpg", "j", 1),
        _file("下載說明.txt", "t", 0),
    ])
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "IDBD-939", "series", dry_run=False)
    assert result["action"] == "flatten"
    # Both discs moved out and named _1/_2; only real junk trashed.
    assert sorted(i for ids, _p in svc.moved for i in ids) == ["d1", "d2"]
    assert sorted(svc.renamed) == [("d1", "IDBD-939_1.mp4"),
                                   ("d2", "IDBD-939_2.mp4")]
    assert sorted(svc.trashed) == ["j", "t", "wrap"]


async def test_none_size_disc_still_kept_as_part(monkeypatch):
    # PikPak can list a real disc with size=None (#220/#225). Collapsing
    # it to 0 broke the "all parts ≥500MB" test → the set was demoted to
    # single-keeper and the None-size disc trashed. None → assume legit,
    # matching finalize's part gate; both discs survive.
    svc = StubSvc([
        _file("idbd-939-1.mp4", "d1", 10300),
        SimpleNamespace(name="idbd-939-2.mp4", id="d2",
                        kind="drive#file", size=None,
                        phase="PHASE_TYPE_COMPLETE"),
    ])
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "IDBD-939", "series", dry_run=False)
    assert result["action"] == "flatten"
    assert sorted(i for ids, _p in svc.moved for i in ids) == ["d1", "d2"]
    assert "d2" not in svc.trashed


async def test_resolution_dup_still_drops_smaller(monkeypatch):
    svc = StubSvc([
        _file("MIDV-001.mp4", "big", 6000),
        _file("MIDV-001 (2).mp4", "small", 400),  # not all ≥500MB → dup
    ])
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "MIDV-001", "series", dry_run=False)
    assert result["action"] == "flatten"
    assert [i for ids, _p in svc.moved for i in ids] == ["big"]
    assert "small" in svc.trashed


async def test_single_video_names_after_code(monkeypatch):
    svc = StubSvc([_file("hhd800.com@MIDV-001.mp4", "v", 6000)])
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "MIDV-001", "series", dry_run=False)
    assert result["action"] == "flatten"
    assert svc.renamed == [("v", "MIDV-001.mp4")]


async def test_transferring_wrapper_is_skipped(monkeypatch):
    svc = StubSvc([
        _file("idbd-939-1.mp4", "d1", 10300),
        _file("idbd-939-2.mp4", "d2", 200, phase="PHASE_TYPE_RUNNING"),
    ])
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "IDBD-939", "series", dry_run=False)
    assert result["action"] == "skip"
    assert result["reason"] == "transferring"
    assert not svc.moved and not svc.renamed and not svc.trashed


async def test_dry_run_mutates_nothing(monkeypatch):
    svc = StubSvc([
        _file("idbd-939-1.mp4", "d1", 10300),
        _file("idbd-939-2.mp4", "d2", 10350),
    ])
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "IDBD-939", "series", dry_run=True)
    assert result["action"] == "flatten"
    assert not svc.moved and not svc.renamed and not svc.trashed


async def test_outlier_old_rip_never_claims_a_part_slot(monkeypatch):
    """SDMU-845 live case: four real discs + a 1.44GB old whole-film rip
    (same canonical, ≥500MB). The rip must not become _5; the marker-
    bearing CD5 keeps its slot."""
    svc = StubSvc([
        _file("[Thz.la]sdmu-845cd1.mp4", "c1", 4490),
        _file("[Thz.la]sdmu-845cd2.mp4", "c2", 4360),
        _file("[Thz.la]sdmu-845cd3.mp4", "c3", 4440),
        _file("[Thz.la]sdmu-845cd4.mp4", "c4", 4390),
        _file("[Thz.la]sdmu-845cd5.mp4", "c5", 4400),
        _file("SDMU-845.mp4", "old", 1440),  # stray low-res rip
    ])
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "SDMU-845", "series", dry_run=False)
    assert result["action"] == "flatten"
    renamed = dict(svc.renamed)
    assert renamed.get("c5") == "SDMU-845_5.mp4"
    assert "old" not in renamed        # never renamed as a part
    assert "old" in svc.trashed        # dropped with the junk


async def test_settling_wrapper_is_left_alone(monkeypatch):
    import app.services.offline_tasks as ot

    async def yes(fid, grace=None):
        return True

    svc = StubSvc([_file("MIDV-001.mp4", "v", 6000)])
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    monkeypatch.setattr(ot, "is_settling", yes)
    result = await reorg._resolve_folder_winner(
        _wrap(), "MIDV-001", "series", dry_run=False)
    assert result == {"action": "skip", "target": "MIDV-001",
                      "reason": "settling"}
    assert not svc.moved and not svc.renamed and not svc.trashed


async def test_rename_avoids_existing_destination_names(monkeypatch):
    """Live case: dest already holds SDMU-845_2.mp4; a new cd2 must not
    be renamed onto the same name (PikPak allows duplicates)."""
    svc = StubSvc(
        [_file("sdmu-845cd1.mp4", "n1", 4400),
         _file("sdmu-845cd2.mp4", "n2", 4400)],
        dest_children=[_file("SDMU-845_2.mp4", "old2", 4300)],
    )
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "SDMU-845", "series", dry_run=False)
    assert result["action"] == "flatten"
    names = [n for _id, n in svc.renamed]
    assert "SDMU-845_2.mp4" not in names   # collision avoided
    assert len(set(names)) == len(names)


async def test_recently_born_files_defer_flatten(monkeypatch):
    """Slow torrents keep materialising files long after the DB grace —
    a file born minutes ago means more may follow. Leave the wrapper."""
    from datetime import datetime, timedelta

    fresh = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    f = _file("MIDV-001.mp4", "v", 6000)
    f.created_time = fresh
    svc = StubSvc([f])
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "MIDV-001", "series", dry_run=False)
    assert result == {"action": "skip", "target": "MIDV-001",
                      "reason": "settling"}
    assert not svc.moved and not svc.trashed


async def test_old_files_do_not_defer_flatten(monkeypatch):
    from datetime import datetime, timedelta

    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    f = _file("MIDV-001.mp4", "v", 6000)
    f.created_time = old
    svc = StubSvc([f])
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "MIDV-001", "series", dry_run=False)
    assert result["action"] == "flatten"


async def test_unsettled_moves_keep_wrapper(monkeypatch):
    """Async-move physics (live losses: DVDMS-129_3, HRV-012_3/_4,
    MTM-010_2/_3): the wrapper must survive the run whose moves just
    happened — only a later, settled pass may take the shell."""
    svc = StubSvc([
        _file("dvdms-129_1.mp4", "d1", 4000),
        _file("dvdms-129_2.mp4", "d2", 3000),
        _file("dvdms-129_3.mp4", "d3", 3990),
        _file("dvdms-129_4.mp4", "d4", 3100),
    ])
    svc.settled = False
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "DVDMS-129", "series", dry_run=False)
    assert result["action"] == "flatten"       # keepers were evacuated
    assert sorted(i for ids, _p in svc.moved for i in ids) == [
        "d1", "d2", "d3", "d4"]
    assert "wrap" not in svc.trashed           # …but the wrapper stays


async def test_evacuated_shell_trashed_once_settled(monkeypatch):
    """A videoless wrapper whose code video already sits loose at the
    destination is a shell — settled gate open → it may finally go."""
    svc = StubSvc(
        [_file("cover.jpg", "j", 1)],
        dest_children=[_file("DVDMS-129_1.mp4", "v", 4000)],
    )
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "DVDMS-129", "series", dry_run=False)
    assert result["action"] == "flatten"
    assert result["reason"] == "殘殼清除"
    assert svc.trashed == ["wrap"]


async def test_evacuated_shell_waits_for_gate(monkeypatch):
    svc = StubSvc(
        [_file("cover.jpg", "j", 1)],
        dest_children=[_file("DVDMS-129_1.mp4", "v", 4000)],
    )
    svc.settled = False
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "DVDMS-129", "series", dry_run=False)
    assert result == {"action": "skip", "target": "DVDMS-129",
                      "reason": "move_settling"}
    assert svc.trashed == []


async def test_ad_video_plus_iso_keeps_container(monkeypatch):
    """Live 2026-07-19 (AP-491/AP-488): wrapper = 40MB ad mp4 + 3.7GB
    ISO + covers. The any-size fallback crowns the ad as keeper; the
    ISO must ride along to the series folder (container-swap's food),
    not be trashed as wrapper junk."""
    svc = StubSvc([
        _file("AP-491.mp4", "ad", 40),
        _file("AP491.ISO", "iso", 3700),
        _file("2ap491pl.jpg", "j", 1),
    ])
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "AP-491", "series", dry_run=False)
    assert result["action"] == "flatten"
    moved = [i for ids, _p in svc.moved for i in ids]
    assert "iso" in moved
    assert "iso" not in svc.trashed
    assert "j" in svc.trashed
    # The container keeps its own name — no rename.
    assert all(fid != "iso" for fid, _n in svc.renamed)


async def test_credible_video_still_retires_container(monkeypatch):
    """A substantial video ≥25% of the container's size fulfils the
    container's purpose — the ISO goes to trash as before."""
    svc = StubSvc([
        _file("MIDV-002.mp4", "vid", 4000),
        _file("MIDV002.ISO", "iso", 4200),
    ])
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "MIDV-002", "series", dry_run=False)
    assert result["action"] == "flatten"
    moved = [i for ids, _p in svc.moved for i in ids]
    assert moved == ["vid"]
    assert "iso" in svc.trashed


async def test_archive_volume_set_rides_along_whole(monkeypatch):
    """rar volume pieces are never individually retired — a size
    contest against one piece is meaningless (mirrors phase-2 #240)."""
    svc = StubSvc([
        _file("SNIS-494.mp4", "ad", 40),
        _file("snis494.part1.rar", "r1", 2000),
        _file("snis494.part2.rar", "r2", 1400),
    ])
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "SNIS-494", "series", dry_run=False)
    assert result["action"] == "flatten"
    moved = [i for ids, _p in svc.moved for i in ids]
    assert "r1" in moved and "r2" in moved
    assert "r1" not in svc.trashed and "r2" not in svc.trashed


async def test_tiny_stray_archive_still_junked(monkeypatch):
    """A sub-300MB zip beside a real video is junk, not a container
    worth feeding to the swap flow."""
    svc = StubSvc([
        _file("MIDV-003.mp4", "vid", 6000),
        _file("字幕包.zip", "z", 5),
    ])
    monkeypatch.setattr(reorg, "pikpak_service", svc)
    result = await reorg._resolve_folder_winner(
        _wrap(), "MIDV-003", "series", dry_run=False)
    assert result["action"] == "flatten"
    assert "z" in svc.trashed
