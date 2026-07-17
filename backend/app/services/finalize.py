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
- a disc image / archive (``CONTAINER_EXTS``) → **trash only**: it is
  not junk, it is the video in a wrapper we cannot play. Permanently
  deleting one was a live hazard — the 9 rescued containers (SNIS-494.iso
  at 23.8GB …) survived only by sitting alone in their series folders,
  where the finalize path never reached them. One landing beside a video
  would have been destroyed with no undo;
- a same-canonical *substantial* video that loses to a bigger sibling
  (resolution re-download) → **trash only** (recoverable ~30 days) —
  that's the one call a heuristic can plausibly get wrong;
- emptied sub-folders → **trash only**, and only after a fresh re-list
  proves no keeper is left inside — a re-list cannot prove a slow
  offline task won't materialise one more disc in there hours later
  (in-flight files are invisible to listings; live losses through the
  old permanent delete: DVDMS-172_2, SDMU-845_6);
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

from .jav_code import CONTAINER_EXTS, ext_of, extract_jav_code, is_video
from .rename_plan import (
    _build_video_rename_plan,
    _split_size_outliers,
    _uniquify_target,
    low_bitrate_copies,
    quality_tagged_copies,
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
    purge_folders: list[Any] = field(default_factory=list)      # verified-empty → trash
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
    - container (.iso/.zip …) → trash (redundant once a video is kept,
      but never destroyed);
    - other non-video file → purge;
    - folder → purge after keepers are evacuated;
    - zero videos anywhere → ``no_video`` abort, all lists empty.
    """
    plan = FinalizePlan()
    folders = [e for e, _p in entries if _is_folder(e)]
    files = [(e, p) for e, p in entries if not _is_folder(e)]
    videos = [(e, p) for e, p in files if is_video_fn(e.name)]
    non_videos = [e for e, _p in files if not is_video_fn(e.name)
                  and ext_of(e.name) not in CONTAINER_EXTS]
    containers = [e for e, _p in files if not is_video_fn(e.name)
                  and ext_of(e.name) in CONTAINER_EXTS]

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
            # Same canonical + all substantial usually means discs, but a
            # member whose name declares an encode (SD/4K tag beside a
            # bare sibling) or whose runtime proves it means encodes of
            # ONE film. A copy never takes a part slot: with ≥2 members
            # left the copies are stray rips beside real discs; otherwise
            # the whole group is one film and only the biggest survives
            # (the 4K upgrade may be the tagged one — SQTE-656 landed as
            # bare-SD + ``-4k``, and dropping by tag alone kept the SD).
            copies = low_bitrate_copies(members)
            copies += [m for m in quality_tagged_copies(members, canon)
                       if m not in copies]
            if copies:
                rest = [m for m in members if m not in copies]
                if len(rest) >= 2:
                    dup_trash.extend(copies)
                    members = rest
                else:
                    members = sorted(members, key=lambda m: (m.size or 0),
                                     reverse=True)
                    keepers.append(members[0])
                    dup_trash.extend(members[1:])
                    continue
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
    elif len(targets) == 1:
        # A lone keeper whose stem is title/BT noise AROUND the code
        # keeps that noise: the canonical pass deliberately leaves a
        # mid-name code untouched, and its rename_map target still
        # parses to our code, so neither path above cleans it (live:
        # 【…】【SEX8.CC】…EKDV-014 スク水H….avi archived verbatim).
        # Inside the code's own folder the code IS the name — mirror
        # _resolve_folder_winner's single-keeper rule. A code-anchored
        # stem (``CODE``/``CODE_2``/``CODEA`` variants) stays as
        # planned, so parts and variant letters are never clobbered.
        k, target = targets[0]
        canon_t = _canonical_video_name(target)
        if (extract_jav_code(target) == code and canon_t != code.upper()
                and not canon_t.startswith(code.upper())):
            targets[0] = (k, _uniquify_target(f"{code}{ext_of(k.name)}",
                                              taken - {target}))

    plan.keep = targets
    plan.move_to_root = [k for k, _t in targets if parent_of.get(k.id) != root_id]
    plan.purge_files = non_videos + ad_purge
    plan.trash_files = dup_trash + containers
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


async def wrapper_is_ad_shell(svc, folder_id: str) -> bool:
    """True when a wrapper folder verifiably holds files but not one
    video or container — an ad shell.

    Some magnets deliver a wrapper of pure ads/screenshots with no film
    at all. Archiving one anyway mints a canonical-looking 番號 folder
    that every layer reads as success, and nothing ever re-sends the
    code (live: EDD-138, then OYC-205). Containers count as content:
    a lone ``CODE.iso`` is the container-swap loop's job, not junk.

    Every "can't tell" answer is False — only a complete, settled
    listing may condemn a folder:
    - truncated listing → a video may sit in the unseen tail;
    - empty listing → PikPak's optimistic listings show freshly moved
      folders as empty while files are still in flight (#140);
    - any file still transferring → judge again once it lands.
    A file id (non-folder) yields an empty listing and lands on the
    empty case, so callers need not pre-check the kind."""
    entries, _folders, partial = await _list_subtree(svc, folder_id)
    if partial:
        return False
    files = [e for e, _pid in entries if not _is_folder(e)]
    if not files:
        return False
    if any(_is_transferring(f) for f in files):
        return False
    return not any(
        is_video(f.name) or ext_of(f.name) in CONTAINER_EXTS for f in files
    )


async def presence_code_folders(svc, code: str) -> list[tuple[str, str, str]]:
    """``(folder_id, leaf_name, path)`` for every *per-code folder* the
    presence index knows about — a path whose leaf is a folder that
    resolves to ``code`` (``[Thz.la]dvdms-129``, ``mtm-010``, …).

    The root sweep moves a wrapper folder wholesale, keeping its BT name
    and choosing the series folder from what's physically on PikPak — so
    the canonical ``製作商/<studio>/<series>/<CODE>`` guess can miss even
    though a per-code folder absolutely exists. Loose-video paths (the
    flattened layout) don't count."""
    from .jav_code import normalize_code  # avoid cycle at import time
    from .pikpak_presence import presence_index

    want = normalize_code(code)
    if not want:
        return []
    hits: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for path in presence_index.paths_for(code):
        leaf = path.rsplit("/", 1)[-1]
        if is_video(leaf):
            continue
        if normalize_code(extract_jav_code(leaf) or "") != want:
            continue
        fid = await svc.lookup_folder_id(path)  # folder-typed leaf only
        if fid and fid not in seen:
            seen.add(fid)
            hits.append((fid, leaf, path))
    return hits


async def _parent_has_code_video(svc, parent_id: str, code: str) -> bool:
    """A substantial video for ``code`` already sits loose in the parent
    (系列) folder — evidence that an earlier settle-gated run evacuated
    this folder's keepers and only a junk shell remains."""
    try:
        kids, _partial = await svc.list_all_files(parent_id)
    except Exception:  # noqa: BLE001
        return False
    want = (extract_jav_code(code) or code).upper()
    return any(
        not _is_folder(k)
        and is_video(k.name)
        and (extract_jav_code(k.name) or "").upper() == want
        and (k.size or 0) >= JUNK_BYTES
        for k in kids
    )


async def finalize_code_folder_stream(
    svc,
    code: str,
    *,
    folder_id: str | None = None,
    dry_run: bool = True,
) -> AsyncIterator[dict]:
    """Finalize one 番號's archive folder. Events mirror the cleanup
    stream: ``start`` / ``progress`` (action ∈ rename|move|purge|trash|
    skip|error) / ``warn`` / ``done``.

    Layout policy: NO per-code folders (user decision 2026-07-14). The
    keepers are evacuated into the folder's PARENT (the 系列 folder) as
    loose ``CODE.ext`` / ``CODE_N.ext`` files and the per-code folder is
    removed once every keeper's arrival at the parent is confirmed. When
    the parent can't be resolved the old keep-the-folder behaviour runs
    instead — never guess a flatten destination."""
    rename_folder_to: str | None = None
    folder_leaf = code
    folder_path: str | None = None
    if folder_id is None:
        from .archiver import _archive_leaf, _resolve_archive_path_by_code

        path = await _resolve_archive_path_by_code(code)
        folder_id = await svc.lookup_folder_id(path)
        folder_path = path if folder_id else None
        if not folder_id:
            hits = await presence_code_folders(svc, code)
            if len(hits) > 1:
                yield {"type": "error",
                       "message": (f"{code} 有 {len(hits)} 個候選資料夾,"
                                   "無法確定要整理哪一個,中止")}
                return
            if hits:
                folder_id, folder_leaf, folder_path = hits[0]
                canonical = _archive_leaf(code)
                if folder_leaf != canonical:
                    rename_folder_to = canonical
        if not folder_id:
            yield {"type": "error", "message": f"找不到 {code} 的歸檔資料夾({path})"}
            return
    else:
        # Explicit folder_id (archiver inline hook): recover the path so
        # the parent (系列 folder) can be resolved for the flatten. Only
        # trust it when it resolves back to the same folder; any failure
        # just means no flatten (legacy keep-the-folder behaviour).
        try:
            from .archiver import _resolve_archive_path_by_code

            path = await _resolve_archive_path_by_code(code)
            if await svc.lookup_folder_id(path) == folder_id:
                folder_path = path
        except Exception as exc:  # noqa: BLE001
            logger.debug("finalize %s: path recovery failed: %s", code, exc)

    # Flatten destination: the folder's parent. None → legacy behaviour.
    parent_id: str | None = None
    if folder_path and "/" in folder_path:
        try:
            parent_id = await svc.lookup_folder_id(
                folder_path.rsplit("/", 1)[0])
        except Exception as exc:  # noqa: BLE001
            logger.debug("finalize %s: parent lookup failed: %s", code, exc)

    entries, folder_depth, partial = await _list_subtree(svc, folder_id)
    parent_of = {e.id: p for e, p in entries}
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
    flatten = parent_id is not None and parent_id != folder_id

    if plan.no_video:
        if flatten and await _parent_has_code_video(svc, parent_id, code):
            # Evacuated shell from an earlier settle-gated run: the
            # videos are confirmed loose at the parent, so everything
            # still in here is junk. Re-plan as deletion-only and let
            # the normal (settle-gated) phases below remove it. Containers
            # still go to the trash, not delete_forever — "a video for this
            # code exists at the parent" does not prove the disc image is
            # the same content.
            leftovers = [e for e, _p in entries if not _is_folder(e)]
            plan = FinalizePlan(
                purge_files=[e for e in leftovers
                             if ext_of(e.name) not in CONTAINER_EXTS],
                trash_files=[e for e in leftovers
                             if ext_of(e.name) in CONTAINER_EXTS],
                purge_folders=[f for f, _d in folder_depth],
            )
        else:
            yield {"type": "warn",
                   "message": f"{code} 資料夾內沒有影片,略過(不做任何刪除)"}
            yield {"type": "done", "result": {
                "kept": 0, "renamed": 0, "moved": 0, "purged": 0,
                "trashed": 0, "skipped": 0, "settling": 0, "errors": 0,
                "dry_run": dry_run, "no_video": True,
            }}
            return

    # Ids this run actually removed. PikPak keeps listing them for a
    # while, so the presence refresh downstream must be told to ignore
    # them rather than race the drive's eventual consistency.
    gone_ids: list[str] = []
    summary = {"kept": len(plan.keep), "renamed": 0, "moved": 0, "purged": 0,
               "trashed": 0, "skipped": 0, "settling": 0, "errors": 0,
               "dry_run": dry_run, "gone_ids": gone_ids}
    renames = [(k, t) for k, t in plan.keep if k.name != t]
    move_ids = ({k.id for k, _t in plan.keep} if flatten
                else {k.id for k in plan.move_to_root})
    total = (len(renames) + len(move_ids) + len(plan.purge_files)
             + len(plan.trash_files) + len(plan.purge_folders)
             + (1 if flatten else 0)
             + (1 if rename_folder_to and not flatten else 0))
    yield {"type": "start", "total": total, "code": code}
    current = 0

    def ev(action: str, source: str, target: str | None = None,
           kind: str = "file", reason: str | None = None) -> dict:
        nonlocal current
        current += 1
        return {"type": "progress", "current": current, "kind": kind,
                "action": action, "source": source, "target": target,
                "reason": reason}

    # Presence-resolved wrapper keeps its BT name — when the folder is
    # staying (no flatten destination) normalise its name so the
    # canonical path resolver finds it next time. Pointless when the
    # flatten below removes the folder anyway. A name collision just
    # skips: the file-level work doesn't depend on the folder's name.
    if rename_folder_to and not flatten:
        try:
            if not dry_run:
                await _retry_transient(
                    lambda: svc.rename_file(folder_id, rename_folder_to))
            summary["renamed"] += 1
            yield ev("rename", folder_leaf, rename_folder_to, kind="folder")
        except Exception as exc:  # noqa: BLE001
            summary["skipped"] += 1
            yield ev("skip", folder_leaf, rename_folder_to, kind="folder",
                     reason=str(exc))

    if plan.skipped_all_clean and not flatten:
        # Already-canonical folder AND no flatten destination — nothing
        # to do. (With a parent resolved the folder itself must still be
        # dissolved, so the fast path doesn't apply.)
        summary["skipped"] = len(plan.keep)
        yield {"type": "done", "result": summary}
        return

    # a. Evacuate keepers — rename, then move to the 系列 folder
    #    (flatten) or pull nested ones up to the root (legacy). Names
    #    must not collide with the destination's existing children.
    #    Any keeper failure aborts BEFORE the destructive phases.
    taken: set[str] = set()
    if flatten:
        try:
            siblings, _p = await svc.list_all_files(parent_id)
            taken = {s.name for s in siblings}
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error",
                   "message": f"無法列出系列資料夾,中止: {exc}"}
            return
    for keeper, target in plan.keep:
        try:
            if flatten:
                target = _uniquify_target(target, taken)
                taken.add(target)
            if keeper.name != target:
                if not dry_run:
                    await _retry_transient(lambda k=keeper, t=target: svc.rename_file(k.id, t))
                summary["renamed"] += 1
                yield ev("rename", keeper.name, target)
            if keeper.id in move_ids:
                dest = parent_id if flatten else folder_id
                if not dry_run:
                    await _retry_transient(
                        lambda k=keeper, d=dest: svc.move_files([k.id], d))
                    # Every folder the keeper leaves behind (its whole
                    # ancestor chain up to and including the code
                    # folder) is now settle-gated against deletion —
                    # async moves die with a deleted source (live
                    # losses: DVDMS-129_3, HRV-012_3/_4, MTM-010_2/_3;
                    # destination sightings proved nothing).
                    pid = parent_of.get(keeper.id)
                    while pid:
                        svc.record_move_source(pid)
                        if pid == folder_id:
                            break
                        pid = parent_of.get(pid)
                    svc.record_move_source(folder_id)
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
                gone_ids.extend(ids)
            summary["purged"] += len(plan.purge_files)
            for e in plan.purge_files:
                yield ev("purge", e.name)
        if plan.trash_files:
            if not dry_run:
                await svc.trash_files([e.id for e in plan.trash_files])
                gone_ids.extend(e.id for e in plan.trash_files)
            summary["trashed"] += len(plan.trash_files)
            for e in plan.trash_files:
                yield ev("trash", e.name,
                         reason=("container"
                                 if ext_of(e.name) in CONTAINER_EXTS
                                 else "duplicate"))
    except Exception as exc:  # noqa: BLE001
        summary["errors"] += 1
        yield {"type": "error", "message": f"刪除垃圾檔失敗: {exc}"}
        yield {"type": "done", "result": summary}
        return

    # c. Sub-folders, deepest first — re-list to prove no keeper (or
    #    anything unplanned) is still inside before removal. ``survivors``
    #    tracks descendants that were skipped or failed: a planned-gone
    #    folder that in fact survived must keep every ancestor alive too,
    #    otherwise removing the ancestor would take the survivor (and
    #    whatever made us skip it) down with it.
    #
    #    Emptied shells go to the TRASH, never delete_forever: a slow
    #    offline task can keep saving a disc for hours after its siblings
    #    finished, and that in-flight file is INVISIBLE to every listing
    #    (PikPak filters phase != COMPLETE), so "re-lists empty" cannot
    #    prove nothing more will materialise inside. A late disc that
    #    lands in a trashed shell is recoverable; in a purged one it is
    #    gone (live losses: DVDMS-172_2, SDMU-845_6).
    keep_ids = {k.id for k, _t in plan.keep}
    planned_gone = ({e.id for e in plan.purge_files}
                    | {e.id for e in plan.trash_files}
                    | {f.id for f, _d in folder_depth})
    survivors: set[str] = set()
    for folder, _depth in sorted(folder_depth, key=lambda fd: -fd[1]):
        try:
            if dry_run:
                summary["trashed"] += 1
                yield ev("trash", folder.name, kind="folder",
                         reason="emptied_shell")
                continue
            # A folder something was just moved OUT of must wait out the
            # settle gate — "lists empty" is not proof while an async
            # move is in flight, and the file dies with the folder.
            if not svc.move_settled(folder.id):
                survivors.add(folder.id)
                summary["settling"] += 1
                yield ev("skip", folder.name, kind="folder",
                         reason="move_settling")
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
            await svc.trash_files([folder.id])
            gone_ids.append(folder.id)
            summary["trashed"] += 1
            yield ev("trash", folder.name, kind="folder",
                     reason="emptied_shell")
        except Exception as exc:  # noqa: BLE001
            survivors.add(folder.id)
            summary["errors"] += 1
            yield ev("error", folder.name, kind="folder", reason=str(exc))

    # d. Flatten epilogue: the per-code folder itself. Keepers are
    #    confirmed at the parent; anything the re-list still shows that
    #    we didn't plan away blocks the removal (survivors included).
    #    Same trash-not-purge rule as the sub-folders above.
    if flatten:
        try:
            if dry_run:
                summary["trashed"] += 1
                yield ev("trash", folder_leaf, kind="folder",
                         reason="emptied_shell")
            elif not svc.move_settled(folder_id):
                # Same async-move physics as the sub-folders: the code
                # folder keepers just left must outlive the settle gate.
                # A later retry pass takes the emptied shell down.
                summary["settling"] += 1
                yield ev("skip", folder_leaf, kind="folder",
                         reason="move_settling")
            else:
                kids, _partial = await svc.list_all_files(folder_id)
                leftover = [c for c in kids
                            if c.id in survivors
                            or c.id not in planned_gone]
                if leftover:
                    summary["skipped"] += 1
                    yield ev("skip", folder_leaf, kind="folder",
                             reason="not_empty")
                else:
                    await svc.trash_files([folder_id])
                    gone_ids.append(folder_id)
                    summary["trashed"] += 1
                    yield ev("trash", folder_leaf, kind="folder",
                             reason="emptied_shell")
        except Exception as exc:  # noqa: BLE001
            summary["errors"] += 1
            yield ev("error", folder_leaf, kind="folder", reason=str(exc))

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
            # The archiver swallows the None return — without this line
            # a failing finalize is invisible (lived through it on
            # DVDMS-306: three silent no-ops, zero log evidence).
            logger.warning("finalize %s: %s", code, event.get("message"))
        elif etype == "warn":
            logger.info("finalize %s: %s", code, event.get("message"))
        elif etype == "done":
            summary = event.get("result")
    if summary is None or failed or summary.get("errors"):
        return None
    if summary.get("no_video"):
        return None  # move may not have landed yet — let the archiver retry
    if summary.get("settling"):
        # Folder deletions are waiting out the move-settle gate — the
        # content is already correct, but keep retrying so the shell is
        # removed once the gate opens.
        return None
    # Finalize is where a code's archived paths take their final shape
    # (renamed keepers, junk gone, wrapper emptied). Push that one code
    # into the persisted index rather than invalidating everything: the
    # index survives restarts now, and a blanket invalidation would only
    # buy a multi-minute full walk on the next read.
    try:
        from .pikpak_presence import presence_index  # avoid cycle

        await presence_index.refresh_codes(
            [code], exclude_ids=set(summary.get("gone_ids") or ())
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("presence refresh after finalize %s failed: %s", code, exc)
    return summary
