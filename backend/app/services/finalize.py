"""Post-download finalize: leave a 番號 folder holding ONLY the real
video(s), canonically named.

The archiver moves a completed PikPak task's wrapper folder wholesale
into ``製作商/<studio>/<系列>/<番號>`` — BT junk (ad clips, .txt/.url,
screenshots, Sample/) rides along and the videos keep their BT names.
This module finishes the job: keep the substantial videos, rename them
``CODE.ext`` / ``CODE_N.ext`` (multi-part), pull them up to the 番號
folder root, and delete everything else.

Deletion is tiered by how reversible a mistake would be:

- non-video files and sub-``JUNK_BYTES`` ad clips → **permanently
  deleted** (user decision: reclaim quota immediately);
- a same-canonical *substantial* video that loses to a bigger sibling
  (resolution re-download) → **trash only** (recoverable ~30 days) —
  that's the one call a heuristic can plausibly get wrong;
- emptied sub-folders → permanently deleted, but only after a fresh
  re-list proves no keeper is left inside;
- the 番號 folder itself and the last remaining video are never touched;
- a tree with **zero videos** aborts without any destructive action
  (the async PikPak move may simply not have landed yet — the archiver
  retries).

Split like ``rename_plan``: :func:`build_finalize_plan` is pure and
unit-testable; :func:`finalize_code_folder_stream` executes a plan
against a :class:`~.pikpak.PikPakService` and streams NDJSON events.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .jav_code import ext_of, extract_jav_code, is_video
from .rename_plan import (
    _build_video_rename_plan,
    _split_size_outliers,
    _uniquify_target,
)

logger = logging.getLogger(__name__)

# Same thresholds as cleanup_folder_stream: real JAV episodes are
# ≥500MB; BT ad clips sit well under 300MB.
JUNK_BYTES = 300 * 1024 * 1024
PART_MIN_BYTES = 500 * 1024 * 1024

# How deep below the 番號 folder we look: 番號夾 → wrapper → Sample/.
MAX_DEPTH = 3

_TRANSIENT_RE = re.compile(r"transmission|reached the limit|too\s*frequent", re.IGNORECASE)
_PURGE_CHUNK = 50


@dataclass
class FinalizePlan:
    """What finalize intends to do. All entries are PikPakFile-shaped
    (``id`` / ``name`` / ``kind`` / ``size`` attributes)."""

    keep: list[tuple[Any, str]] = field(default_factory=list)   # (file, target name)
    move_to_root: list[Any] = field(default_factory=list)       # nested keepers
    purge_files: list[Any] = field(default_factory=list)        # permanent delete
    trash_files: list[Any] = field(default_factory=list)        # recoverable trash
    purge_folders: list[Any] = field(default_factory=list)      # verified-empty delete
    skipped_all_clean: bool = False                             # already canonical
    no_video: bool = False                                      # abort: nothing to keep


def _is_folder(entry: Any) -> bool:
    return getattr(entry, "kind", "") == "drive#folder"


def _is_transferring(entry: Any) -> bool:
    """True when the file's own offline-download phase says PikPak is
    still writing it. Empty phase (uploads, old files) counts as done."""
    phase = getattr(entry, "phase", "") or ""
    return bool(phase) and phase != "PHASE_TYPE_COMPLETE"


def build_finalize_plan(
    code: str,
    entries: list[tuple[Any, str]],
    root_id: str,
    *,
    junk_bytes: int = JUNK_BYTES,
    part_min: int = PART_MIN_BYTES,
    is_video_fn=is_video,
) -> FinalizePlan:
    """Classify every descendant of a 番號 folder.

    ``entries`` is the flattened subtree as ``(entry, parent_id)`` pairs
    (the root folder itself excluded). Decision table:

    - video, member of a 2+ group all ≥ ``part_min`` → keep as parts;
    - video, biggest of its canonical group and ≥ ``junk_bytes`` → keep;
    - video, smaller same-canonical sibling ≥ ``junk_bytes`` → trash
      (resolution dup — recoverable on purpose);
    - video < ``junk_bytes`` → purge (ad clip), unless nothing else
      would be kept — the last video always survives;
    - non-video file → purge;
    - folder → purge after keepers are evacuated;
    - zero videos anywhere → ``no_video`` abort, all lists empty.
    """
    plan = FinalizePlan()
    folders = [e for e, _p in entries if _is_folder(e)]
    files = [(e, p) for e, p in entries if not _is_folder(e)]
    videos = [(e, p) for e, p in files if is_video_fn(e.name)]
    non_videos = [e for e, _p in files if not is_video_fn(e.name)]

    if not videos:
        plan.no_video = True
        return plan

    # ---- pick keepers ---------------------------------------------------
    from .rename_plan import _canonical_video_name  # local: keep import surface tiny

    groups: dict[str, list[Any]] = {}
    for e, _p in videos:
        groups.setdefault(_canonical_video_name(e.name), []).append(e)

    keepers: list[Any] = []
    dup_trash: list[Any] = []
    ad_purge: list[Any] = []
    for canon, members in groups.items():
        if len(members) >= 2 and all((m.size or 0) >= part_min for m in members):
            parts, outliers = _split_size_outliers(members, canon)
            keepers.extend(parts)  # genuine multi-part set
            dup_trash.extend(outliers)  # stray whole-film rip → recoverable
            continue
        members = sorted(members, key=lambda m: (m.size or 0), reverse=True)
        best, rest = members[0], members[1:]
        if (best.size or 0) >= junk_bytes or best.size is None:
            keepers.append(best)
        else:
            ad_purge.append(best)
        for m in rest:
            (dup_trash if (m.size or 0) >= junk_bytes else ad_purge).append(m)

    if not keepers:
        # Every video looked like an ad — keep the largest anyway; the
        # last video is never deleted.
        ad_purge.sort(key=lambda m: (m.size or 0), reverse=True)
        keepers.append(ad_purge.pop(0))

    # ---- name keepers ----------------------------------------------------
    rename_map, _members = _build_video_rename_plan(keepers, part_min, is_video_fn)
    parent_of = {e.id: p for e, p in videos}
    taken: set[str] = set()
    targets: list[tuple[Any, str]] = []
    no_code: list[Any] = []
    for k in keepers:
        target = rename_map.get(k.name, k.name)
        if extract_jav_code(target) is None:
            no_code.append(k)  # name carries no 番號 → fall back to ours
            continue
        target = _uniquify_target(target, taken)
        taken.add(target)
        targets.append((k, target))
    if no_code:
        no_code.sort(key=lambda m: (-(m.size or 0), m.name))
        multi = len(no_code) + len(targets) > 1
        for i, k in enumerate(no_code, start=1):
            base = f"{code}_{i}" if multi else code
            target = _uniquify_target(f"{base}{ext_of(k.name)}", taken)
            taken.add(target)
            targets.append((k, target))

    plan.keep = targets
    plan.move_to_root = [k for k, _t in targets if parent_of.get(k.id) != root_id]
    plan.purge_files = non_videos + ad_purge
    plan.trash_files = dup_trash
    plan.purge_folders = folders
    plan.skipped_all_clean = (
        not folders
        and not plan.purge_files
        and not plan.trash_files
        and not plan.move_to_root
        and all(k.name == t for k, t in targets)
    )
    return plan


async def _retry_transient(fn, *, attempts: int = 3, delays: tuple = (10, 30)):
    """Run ``fn`` retrying PikPak's transient move/transmission-limit
    errors with short backoff. Non-transient errors propagate at once."""
    for i in range(attempts):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001
            if i + 1 >= attempts or not _TRANSIENT_RE.search(str(exc)):
                raise
            await asyncio.sleep(delays[min(i, len(delays) - 1)])
    return None  # pragma: no cover — loop always returns or raises


async def _list_subtree(
    svc, folder_id: str, *, max_depth: int = MAX_DEPTH
) -> tuple[list[tuple[Any, str]], list[tuple[Any, int]], bool]:
    """Flatten ``folder_id``'s descendants into ``(entry, parent_id)``
    pairs, plus each folder with its depth (for deepest-first deletes)
    and whether any listing came back truncated."""
    out: list[tuple[Any, str]] = []
    folder_depth: list[tuple[Any, int]] = []
    partial_any = False

    async def walk(fid: str, depth: int) -> None:
        nonlocal partial_any
        kids, partial = await svc.list_all_files(fid)
        partial_any = partial_any or bool(partial)
        for c in kids:
            out.append((c, fid))
            if _is_folder(c):
                folder_depth.append((c, depth))
                if depth < max_depth:
                    await walk(c.id, depth + 1)

    await walk(folder_id, 1)
    return out, folder_depth, partial_any


async def finalize_code_folder_stream(
    svc,
    code: str,
    *,
    folder_id: str | None = None,
    dry_run: bool = True,
) -> AsyncIterator[dict]:
    """Finalize one 番號's archive folder. Events mirror the cleanup
    stream: ``start`` / ``progress`` (action ∈ rename|move|purge|trash|
    skip|error) / ``warn`` / ``done``."""
    if folder_id is None:
        from .archiver import _resolve_archive_path_by_code  # avoid cycle

        path = await _resolve_archive_path_by_code(code)
        folder_id = await svc.lookup_folder_id(path)
        if not folder_id:
            yield {"type": "error", "message": f"找不到 {code} 的歸檔資料夾({path})"}
            return

    entries, folder_depth, partial = await _list_subtree(svc, folder_id)
    if partial:
        # An incomplete inventory could mis-plan a permanent delete.
        yield {"type": "error",
               "message": f"{code} 資料夾列表不完整,為安全起見中止"}
        return
    # Any file still being written by PikPak (offline task not done for
    # THAT file) makes the whole tree off-limits: a half-transferred
    # second disc is indistinguishable from a sub-300MB ad clip. The
    # task-list guard upstream can't be trusted alone — offline_list is
    # observed to return empty while transfers are in flight.
    transferring = [e for e, _p in entries if _is_transferring(e)]
    if transferring:
        yield {"type": "error",
               "message": (f"{code} 還有 {len(transferring)} 個檔案在傳輸中"
                           f"({transferring[0].name}),稍後重試")}
        return
    plan = build_finalize_plan(code, entries, folder_id)

    if plan.no_video:
        yield {"type": "warn", "message": f"{code} 資料夾內沒有影片,略過(不做任何刪除)"}
        yield {"type": "done", "result": {
            "kept": 0, "renamed": 0, "moved": 0, "purged": 0, "trashed": 0,
            "skipped": 0, "errors": 0, "dry_run": dry_run, "no_video": True,
        }}
        return

    summary = {"kept": len(plan.keep), "renamed": 0, "moved": 0, "purged": 0,
               "trashed": 0, "skipped": 0, "errors": 0, "dry_run": dry_run}
    renames = [(k, t) for k, t in plan.keep if k.name != t]
    move_ids = {k.id for k in plan.move_to_root}
    total = (len(renames) + len(plan.move_to_root) + len(plan.purge_files)
             + len(plan.trash_files) + len(plan.purge_folders))
    yield {"type": "start", "total": total, "code": code}
    current = 0

    def ev(action: str, source: str, target: str | None = None,
           kind: str = "file", reason: str | None = None) -> dict:
        nonlocal current
        current += 1
        return {"type": "progress", "current": current, "kind": kind,
                "action": action, "source": source, "target": target,
                "reason": reason}

    if plan.skipped_all_clean:
        summary["skipped"] = len(plan.keep)
        yield {"type": "done", "result": summary}
        return

    # a. Evacuate keepers: rename, then pull nested ones up to the root.
    #    Any keeper failure aborts BEFORE the destructive phases.
    for keeper, target in plan.keep:
        try:
            if keeper.name != target:
                if not dry_run:
                    await _retry_transient(lambda k=keeper, t=target: svc.rename_file(k.id, t))
                summary["renamed"] += 1
                yield ev("rename", keeper.name, target)
            if keeper.id in move_ids:
                if not dry_run:
                    await _retry_transient(lambda k=keeper: svc.move_files([k.id], folder_id))
                summary["moved"] += 1
                yield ev("move", target, None)
        except Exception as exc:  # noqa: BLE001
            summary["errors"] += 1
            yield ev("error", keeper.name, None, reason=str(exc))
            yield {"type": "error",
                   "message": f"影片撤離失敗,中止刪除以免誤刪: {exc}"}
            yield {"type": "done", "result": summary}
            return

    # b. Junk: ads + non-videos permanently, resolution dups to trash.
    try:
        if plan.purge_files:
            if not dry_run:
                ids = [e.id for e in plan.purge_files]
                for i in range(0, len(ids), _PURGE_CHUNK):
                    await svc.delete_forever(ids[i:i + _PURGE_CHUNK])
            summary["purged"] += len(plan.purge_files)
            for e in plan.purge_files:
                yield ev("purge", e.name)
        if plan.trash_files:
            if not dry_run:
                await svc.trash_files([e.id for e in plan.trash_files])
            summary["trashed"] += len(plan.trash_files)
            for e in plan.trash_files:
                yield ev("trash", e.name, reason="duplicate")
    except Exception as exc:  # noqa: BLE001
        summary["errors"] += 1
        yield {"type": "error", "message": f"刪除垃圾檔失敗: {exc}"}
        yield {"type": "done", "result": summary}
        return

    # c. Sub-folders, deepest first — re-list to prove no keeper (or
    #    anything unplanned) is still inside before the permanent delete.
    #    ``survivors`` tracks descendants that were skipped or failed:
    #    a planned-gone folder that in fact survived must keep every
    #    ancestor alive too, otherwise purging the ancestor would take
    #    the survivor (and whatever made us skip it) down with it.
    keep_ids = {k.id for k, _t in plan.keep}
    planned_gone = ({e.id for e in plan.purge_files}
                    | {e.id for e in plan.trash_files}
                    | {f.id for f, _d in folder_depth})
    survivors: set[str] = set()
    for folder, _depth in sorted(folder_depth, key=lambda fd: -fd[1]):
        try:
            if dry_run:
                summary["purged"] += 1
                yield ev("purge", folder.name, kind="folder")
                continue
            kids, _partial = await svc.list_all_files(folder.id)
            leftover = [c for c in kids
                        if c.id in keep_ids
                        or c.id in survivors
                        or c.id not in planned_gone]
            if leftover:
                survivors.add(folder.id)
                summary["skipped"] += 1
                yield ev("skip", folder.name, kind="folder", reason="not_empty")
                continue
            await svc.delete_forever([folder.id])
            summary["purged"] += 1
            yield ev("purge", folder.name, kind="folder")
        except Exception as exc:  # noqa: BLE001
            survivors.add(folder.id)
            summary["errors"] += 1
            yield ev("error", folder.name, kind="folder", reason=str(exc))

    yield {"type": "done", "result": summary}


async def run_finalize(svc, code: str, *, folder_id: str | None = None) -> dict | None:
    """Drain the stream non-interactively (archiver hook). Returns the
    ``done`` summary when finalize fully succeeded, else ``None``."""
    summary: dict | None = None
    failed = False
    async for event in finalize_code_folder_stream(
        svc, code, folder_id=folder_id, dry_run=False
    ):
        etype = event.get("type")
        if etype == "error":
            failed = True
        elif etype == "done":
            summary = event.get("result")
    if summary is None or failed or summary.get("errors"):
        return None
    if summary.get("no_video"):
        return None  # move may not have landed yet — let the archiver retry
    return summary
