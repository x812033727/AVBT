"""Retire the collision copies an upgrade download leaves behind.

When a second magnet for a code lands in a folder that already holds
``CODE.mp4``, PikPak does not overwrite: it stores ``CODE(1).mp4``. The
pipeline has no owner for that leftover, so 112 of them accumulated
(live 2026-07-16) — 99 of the 112 codes provably from two distinct magnets.
The pairs are two copies of the whole film, not two discs, and the
``(N)`` suffix is what proves it: PikPak only appends it when a file
claims a name that is taken, so both files claimed the *same* identity.
A real multi-disc set is ``CODE_1`` / ``CODE_2`` and never collides.

Which to keep is not a judgement call. The backfill only sends a magnet
that is ≥1.8× what we already have, so the newcomer is meant to *replace*
the old copy — it just never did (live: SONE-092 8.16GB vs its 26.86GB
upgrade; ABF-010 7.76GB vs an identical 7.76GB re-download). Keep the
biggest, trash the rest.

Two cadences, on purpose. A pass trashes the losers; the *next* one
renames the surviving ``CODE(1).mp4`` to ``CODE.mp4``, because renaming
into a name a just-trashed file still occupies is exactly the kind of
race the async-listing rule warns about. Nothing here is destructive
beyond the trash — the 30-day undo covers a wrong call.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from ..config import all_kind_paths
from .jav_code import ext_of, extract_jav_code, is_video

logger = logging.getLogger(__name__)

# PikPak's collision suffix: "ABF-002(1)", "REBD-1013 (2)". A stem that
# is anything more than the bare code plus this — "CODE_1" (a real disc),
# "SQTE-659_4KS", "[88K.ME]MIDV-001" — is not a plain copy and is left to
# the normal rename plan.
_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")
_LIST_CONCURRENCY = 5
_TRASH_BATCH = 50


def collision_code(name: str) -> str:
    """The code this file is a plain copy of, or "" when it isn't one.

    A file only qualifies when its stem is the code itself, give or take
    PikPak's ``(N)``. Anything carrying a part index, a resolution tag or
    BT noise is left to the normal rename plan.
    """
    if not is_video(name):
        return ""
    ext = ext_of(name)
    stem = name[: -len(ext)] if ext else name
    bare = _SUFFIX.sub("", stem).strip()
    code = extract_jav_code(bare)
    if not code or bare.upper() != code.upper():
        return ""
    return code


def plan_group(entries: list) -> tuple[list, Any | None]:
    """(losers to trash, file to rename) for one code's copies.

    Only fully-written files are considered: touching one PikPak is still
    writing kills the transfer (#129), and a half-landed file reads small
    enough to lose a size contest it should have won.
    """
    ready = [e for e in entries
             if (getattr(e, "phase", "") or "") in ("", "PHASE_TYPE_COMPLETE")]
    if not ready:
        return [], None
    # An unknown size cannot lose a size contest: PikPak lists real files
    # with size=None (#220/#225), and collapsing that to 0 made a genuine
    # upgrade the guaranteed loser — trashed, then the stale small copy
    # renamed over it. Defer the whole group until every size is known.
    if any(e.size is None for e in ready):
        return [], None
    # Biggest wins; name breaks ties so the choice is stable across runs.
    ranked = sorted(ready, key=lambda e: (-(e.size or 0), e.name))
    winner, losers = ranked[0], ranked[1:]
    if losers:
        return losers, None          # rename waits for the trash to settle
    code = collision_code(winner.name)
    target = f"{code}{ext_of(winner.name)}"
    return [], (winner if winner.name != target else None)


async def sweep_dup_copies_stream(
    svc, *, dry_run: bool = True
) -> AsyncIterator[dict[str, Any]]:
    """Walk 製作商/<studio>/<series> and retire collision copies."""
    studio_path = next((p for k, p in all_kind_paths() if k == "studio"), "")
    root_id = ""
    if studio_path:
        try:
            root_id = await svc.lookup_folder_id(studio_path)
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "message": f"解析 {studio_path} 失敗: {exc}"}
    if not root_id:
        yield {"type": "done", "result": {"scanned": 0, "trashed": 0,
                                          "renamed": 0, "dry_run": dry_run}}
        return

    sem = asyncio.Semaphore(_LIST_CONCURRENCY)

    async def ls(folder_id: str) -> list:
        async with sem:
            try:
                files, _partial = await svc.list_all_files(folder_id, cap=5000)
                return files
            except Exception as exc:  # noqa: BLE001
                logger.debug("dup copies list %s failed: %s", folder_id, exc)
                return []

    trash: list[tuple[Any, str]] = []
    renames: list[tuple[Any, str, str]] = []
    scanned = 0
    for studio in await ls(root_id):
        if studio.kind != "drive#folder":
            continue
        for series in await ls(studio.id):
            if series.kind != "drive#folder":
                continue
            base = f"{studio_path}/{studio.name}/{series.name}"
            # Group per folder, not globally: two copies in two different
            # series folders are the drift problem, not this one, and
            # trashing across folders would delete a file whose folder we
            # never examined.
            groups: dict[str, list] = {}
            for f in await ls(series.id):
                if f.kind == "drive#folder":
                    continue
                scanned += 1
                code = collision_code(f.name)
                if code:
                    groups.setdefault(code, []).append(f)
            for code, entries in groups.items():
                losers, rename = plan_group(entries)
                trash.extend((e, f"{base}/{e.name}") for e in losers)
                if rename is not None:
                    renames.append(
                        (rename, f"{base}/{rename.name}",
                         f"{code}{ext_of(rename.name)}"))

    for _e, path in trash:
        yield {"type": "progress", "action": "trash", "target": path}
    for _e, path, target in renames:
        yield {"type": "progress", "action": "rename", "target": path,
               "to": target}

    errors = 0
    if not dry_run:
        ids = [e.id for e, _p in trash]
        for i in range(0, len(ids), _TRASH_BATCH):
            try:
                await svc.trash_files(ids[i:i + _TRASH_BATCH])
            except Exception as exc:  # noqa: BLE001
                errors += 1
                yield {"type": "error", "message": f"trash 失敗: {exc}"}
        for e, _p, target in renames:
            try:
                await svc.rename_file(e.id, target)
            except Exception as exc:  # noqa: BLE001
                errors += 1
                yield {"type": "error", "message": f"rename {target} 失敗: {exc}"}
        logger.info("dup copies: trashed %d, renamed %d",
                    len(trash), len(renames))

    yield {"type": "done", "result": {
        "scanned": scanned, "trashed": len(trash), "renamed": len(renames),
        "errors": errors, "dry_run": dry_run}}


async def sweep_dup_copies(svc, *, dry_run: bool = True) -> dict[str, Any]:
    """Non-streaming wrapper for the archiver loop / cron."""
    summary: dict[str, Any] = {}
    async for ev in sweep_dup_copies_stream(svc, dry_run=dry_run):
        if ev.get("type") == "done":
            summary = ev.get("result") or {}
    return summary
