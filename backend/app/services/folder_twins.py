"""Merge series folders that are the same folder twice.

Two ways one series ends up with two folders under a studio:

- **name drift** — JavBus returns "新人NO.1 STYLE" one day and
  "新人NO.1STYLE" the next, and exact-match resolution forked a twin
  (that source is closed: resolution now reuses a folder whose
  ``folder_key`` matches);
- **a create race** — two archives resolve the same missing path at once,
  both see "absent", both create. PikPak allows duplicate folder names,
  so this leaves two folders with the *identical* name (live: Aircontrol
  has "ALL NUDE" twice, プレステージ has "絶対的美少女、お貸しします。"
  twice).

Either way the damage is the same and it is worse than untidiness: a
path resolves to exactly one of the twins, so everything in the other is
invisible to every path-based read. MAS-096.iso sat in a 1-file duplicate
of a 65-file folder and was reported as *not archived* — the swap loop
called it a phantom.

Merging removes the ambiguity: biggest folder wins, everything else moves
into it. The shells are NOT deleted here. A move is asynchronous and its
listing optimistic — the file stops listing at the source while the
physical move is still in flight, and deleting the source in that window
destroys it (DVDMS-129_3, HRV-012_3…). Only the 30-minute settle gate
proves otherwise, so a later pass removes the shell once
``move_settled`` says so.
"""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from collections.abc import AsyncIterator
from typing import Any

from ..config import all_kind_paths
from .jav_code import folder_key

logger = logging.getLogger(__name__)

_LIST_CONCURRENCY = 5
_MOVE_BATCH = 50


def _display(name: str) -> str:
    return unicodedata.normalize("NFKC", name)


def plan_merge(folders: list[tuple[Any, list]]) -> tuple[Any, list] | None:
    """(winner, losers) for one twin group, or None when there's nothing
    to merge. The folder holding the most already wins on move count
    alone; name breaks ties so reruns agree."""
    if len(folders) < 2:
        return None
    ranked = sorted(folders, key=lambda t: (-len(t[1]), t[0].name, t[0].id))
    return ranked[0][0], [f for f, _ch in ranked[1:]]


def movable(children: list) -> tuple[list, list]:
    """(safe to move, must be left) for one loser's children.

    A file PikPak is still writing dies if it is moved — folder-level
    moves are safe but file-level ones are not (#129), and an in-flight
    file is invisible to listings anyway, so its wrapper folder must stay
    put too. Those come back on the next pass, once they've landed.
    """
    safe, blocked = [], []
    for c in children:
        phase = getattr(c, "phase", "") or ""
        if phase and phase != "PHASE_TYPE_COMPLETE":
            blocked.append(c)
        else:
            safe.append(c)
    return safe, blocked


async def merge_folder_twins_stream(
    svc, *, dry_run: bool = True
) -> AsyncIterator[dict[str, Any]]:
    """Walk 製作商/<studio>, merge each group of twin series folders."""
    studio_path = next((p for k, p in all_kind_paths() if k == "studio"), "")
    root_id = ""
    if studio_path:
        try:
            root_id = await svc.lookup_folder_id(studio_path)
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "message": f"解析 {studio_path} 失敗: {exc}"}
    if not root_id:
        yield {"type": "done", "result": {"groups": 0, "moved": 0, "skipped": 0,
                                          "shells": 0, "errors": 0,
                                          "dry_run": dry_run}}
        return

    sem = asyncio.Semaphore(_LIST_CONCURRENCY)

    async def ls(folder_id: str) -> list:
        async with sem:
            try:
                files, _partial = await svc.list_all_files(folder_id, cap=5000)
                return files
            except Exception as exc:  # noqa: BLE001
                logger.debug("twins list %s failed: %s", folder_id, exc)
                return []

    groups = moved = skipped = shells = errors = 0
    for studio in await ls(root_id):
        if studio.kind != "drive#folder":
            continue
        by_key: dict[str, list] = {}
        for series in await ls(studio.id):
            if series.kind == "drive#folder":
                by_key.setdefault(folder_key(series.name), []).append(series)

        for twins in by_key.values():
            if len(twins) < 2:
                continue
            with_children = [(f, await ls(f.id)) for f in twins]
            plan = plan_merge(with_children)
            if plan is None:
                continue
            winner, losers = plan
            groups += 1
            children_of = {f.id: ch for f, ch in with_children}
            taken = {c.name for c in children_of[winner.id]}
            yield {"type": "progress", "action": "merge",
                   "target": f"{studio_path}/{studio.name}/{_display(winner.name)}",
                   "keeps": len(children_of[winner.id])}

            for loser in losers:
                children = children_of[loser.id]
                safe, blocked = movable(children)
                for c in blocked:
                    skipped += 1
                    yield {"type": "skip", "target": c.name,
                           "reason": "in_flight"}
                # A name the winner already holds would land as "NAME(1)"
                # — the very collision the dup-copies sweep just cleaned
                # up. Leave it; a human (or that sweep, once the names are
                # canonical) can decide which copy wins.
                clash = [c for c in safe if c.name in taken]
                for c in clash:
                    skipped += 1
                    yield {"type": "skip", "target": c.name, "reason": "name_taken"}
                movers = [c for c in safe if c.name not in taken]

                if movers and not dry_run:
                    ids = [c.id for c in movers]
                    for i in range(0, len(ids), _MOVE_BATCH):
                        try:
                            await svc.move_files(ids[i:i + _MOVE_BATCH], winner.id)
                        except Exception as exc:  # noqa: BLE001
                            errors += 1
                            yield {"type": "error",
                                   "message": f"move {loser.name} 失敗: {exc}"}
                    svc.record_move_source(loser.id)
                moved += len(movers)
                taken.update(c.name for c in movers)
                for c in movers:
                    yield {"type": "progress", "action": "move",
                           "target": c.name,
                           "to": f"{studio_path}/{studio.name}/{_display(winner.name)}"}

                # The shell only goes once the gate proves the moves
                # physically landed; until then it must stay, empty or not.
                emptied = not blocked and not clash
                if emptied and not dry_run and svc.move_settled(loser.id):
                    try:
                        await svc.trash_files([loser.id])
                        shells += 1
                        yield {"type": "progress", "action": "trash",
                               "target": loser.name, "reason": "emptied_shell"}
                    except Exception as exc:  # noqa: BLE001
                        errors += 1
                        yield {"type": "error",
                               "message": f"trash 殼夾 {loser.name} 失敗: {exc}"}
                elif emptied:
                    yield {"type": "skip", "target": loser.name,
                           "reason": "move_settling"}

    yield {"type": "done", "result": {
        "groups": groups, "moved": moved, "skipped": skipped,
        "shells": shells, "errors": errors, "dry_run": dry_run}}


async def merge_folder_twins(svc, *, dry_run: bool = True) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    async for ev in merge_folder_twins_stream(svc, dry_run=dry_run):
        if ev.get("type") == "done":
            summary = ev.get("result") or {}
    return summary
