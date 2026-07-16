"""Twin series folders: same series, two folders, so half its files are
invisible to every path-based read."""

from types import SimpleNamespace

from app.services.folder_twins import merge_folder_twins, movable, plan_merge

GB = 1024 ** 3


def _f(name, fid, kind="drive#folder", phase="PHASE_TYPE_COMPLETE", size=None):
    return SimpleNamespace(name=name, id=fid, kind=kind, phase=phase, size=size)


def test_the_fuller_folder_wins():
    big = (_f("新人NO.1 STYLE", "big"), [_f("a", "a1")] * 210)
    small = (_f("新人NO.1STYLE", "small"), [_f("b", "b1")] * 5)
    winner, losers = plan_merge([small, big])
    assert winner.id == "big" and [f.id for f in losers] == ["small"]


def test_three_way_group_merges_into_one():
    # Aircontrol live: ALLNUDE(34), "ALL NUDE"(42) and a second, empty
    # "ALL NUDE" — drift and a create race in the same group.
    a = (_f("ALLNUDE", "a"), [_f("x", "x")] * 34)
    b = (_f("ALL NUDE", "b"), [_f("y", "y")] * 42)
    c = (_f("ALL NUDE", "c"), [])
    winner, losers = plan_merge([a, b, c])
    assert winner.id == "b" and sorted(f.id for f in losers) == ["a", "c"]


def test_ties_resolve_the_same_way_every_run():
    a = (_f("ALL NUDE", "a"), [])
    b = (_f("ALLNUDE", "b"), [])
    assert plan_merge([a, b])[0].id == plan_merge([b, a])[0].id


def test_a_lone_folder_is_not_a_group():
    assert plan_merge([(_f("ALL NUDE", "a"), [])]) is None


def test_in_flight_children_are_not_movable():
    # Moving a file PikPak is still writing kills it (#129).
    done = _f("SNIS-494.avi", "d", kind="drive#file")
    landing = _f("SOE-789", "l", phase="PHASE_TYPE_RUNNING")
    safe, blocked = movable([done, landing])
    assert [f.id for f in safe] == ["d"]
    assert [f.id for f in blocked] == ["l"]


class FakeSvc:
    def __init__(self, tree, settled=True):
        self.tree = tree
        self.moved: list[tuple[str, str]] = []
        self.trashed: list[str] = []
        self.sources: list[str] = []
        self._settled = settled

    async def lookup_folder_id(self, path):
        return "studio-root" if path.endswith("製作商") else ""

    async def list_all_files(self, folder_id, cap=5000):
        return list(self.tree.get(folder_id, [])), False

    async def move_files(self, ids, to_parent_id):
        self.moved.extend((i, to_parent_id) for i in ids)
        return {}

    async def trash_files(self, ids):
        self.trashed.extend(ids)
        return {}

    def record_move_source(self, source_id):
        self.sources.append(source_id)

    def move_settled(self, source_id):
        return self._settled


def _tree():
    return {
        "studio-root": [_f("エスワン", "s1")],
        "s1": [_f("新人NO.1 STYLE", "keep"), _f("新人NO.1STYLE", "drop")],
        # The winner is whichever holds more — keep it that way here, or
        # the fixture quietly tests the merge running backwards.
        "keep": [_f("SOE-453.mp4", "k1", kind="drive#file", size=3 * GB),
                 _f("SOE-887.wmv", "k2", kind="drive#file", size=3 * GB),
                 _f("SOE-522.wmv", "k3", kind="drive#file", size=3 * GB)],
        "drop": [_f("SNIS-494.avi", "d1", kind="drive#file", size=2 * GB),
                 _f("SOE-789", "d2")],
    }


async def test_merge_moves_the_loser_into_the_winner():
    svc = FakeSvc(_tree())
    summary = await merge_folder_twins(svc, dry_run=False)
    assert sorted(svc.moved) == [("d1", "keep"), ("d2", "keep")]
    assert svc.sources == ["drop"]          # the settle gate needs to know
    assert summary["groups"] == 1 and summary["moved"] == 2


async def test_shell_waits_for_the_settle_gate():
    # A move is async and its listing optimistic: deleting the source
    # while the move is in flight destroys the file (DVDMS-129_3).
    svc = FakeSvc(_tree(), settled=False)
    await merge_folder_twins(svc, dry_run=False)
    assert svc.trashed == []


async def test_shell_goes_once_the_gate_opens():
    svc = FakeSvc(_tree(), settled=True)
    summary = await merge_folder_twins(svc, dry_run=False)
    assert svc.trashed == ["drop"] and summary["shells"] == 1


async def test_a_name_the_winner_holds_is_left_behind():
    # Moving it would land as "NAME(1)" — recreating the collision the
    # dup-copies sweep exists to clean up.
    tree = _tree()
    tree["drop"] = [_f("SOE-453.mp4", "clash", kind="drive#file", size=1 * GB)]
    svc = FakeSvc(tree)
    summary = await merge_folder_twins(svc, dry_run=False)
    assert svc.moved == []
    assert summary["skipped"] == 1
    assert svc.trashed == []                # shell still holds something


async def test_an_in_flight_child_keeps_its_shell_alive():
    tree = _tree()
    tree["drop"] = [_f("SOE-789", "landing", phase="PHASE_TYPE_RUNNING")]
    svc = FakeSvc(tree)
    summary = await merge_folder_twins(svc, dry_run=False)
    assert svc.moved == [] and svc.trashed == []
    assert summary["skipped"] == 1


async def test_dry_run_touches_nothing():
    svc = FakeSvc(_tree())
    summary = await merge_folder_twins(svc, dry_run=True)
    assert svc.moved == [] and svc.trashed == [] and svc.sources == []
    assert summary["moved"] == 2 and summary["dry_run"] is True
