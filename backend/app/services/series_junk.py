"""Sweep BT junk that landed loose in a 系列 folder.

``finalize`` purges the ad clips / txt / screenshots that ride along with
a torrent — but only inside a 番號 folder. The sweep's flatten layout
puts the video straight into ``製作商/<studio>/<series>/``, and when the
wrapper's junk comes along for the ride nothing ever cleans it: the
finalize retry pass sees the code is already flattened and just stamps
the row (live find 2026-07-16: 111 ad clips totalling 3.5GB across the
library — ``社 区 最 新 情 报.mp4``, ``威尼斯人_真人棋牌…mp4``,
``約會神器.mp4`` …). User authorised the library-wide sweep.

Rules, deliberately conservative:

- **junk** = a video under ``JUNK_BYTES``, or a file that is not a video
  and not a container (``CONTAINER_EXTS``)
- a container (``.iso``/``.zip``; e.g. the rescued SNIS-494.iso at
  23.8GB) is the video in disguise, so it survives — *until* a real
  playable video for the same code lands beside it, which is what the
  container swap arranges. Then it is redundant and gets trashed, which
  is the tail end of the swap: download the video, drop the disc image.
- a file PikPak is still writing (``phase`` not COMPLETE) is never
  touched — that is the #129 rule: touching an in-flight file kills the
  transfer and the partial vanishes
- junk goes to the **trash**, not ``delete_forever``: this pass judges by
  size alone, without the task's magnet to compare against, so it keeps
  the 30-day undo every manual cleanup here keeps
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from ..config import all_kind_paths
from .jav_code import CONTAINER_EXTS, ext_of, extract_jav_code, is_video

logger = logging.getLogger(__name__)

JUNK_BYTES = 300 * 1024 * 1024
_LIST_CONCURRENCY = 5
_TRASH_BATCH = 50


def is_series_junk(
    name: str,
    size: int | None,
    phase: str = "",
    *,
    code_has_video: bool = False,
) -> bool:
    """Whether a file sitting directly in a 系列 folder is BT junk.

    ``code_has_video`` says a real playable video for this file's code is
    already in the same folder; it only ever promotes a container to junk.
    """
    if phase not in ("", "PHASE_TYPE_COMPLETE"):
        return False  # still being written — hands off (#129)
    if not is_video(name):
        if ext_of(name) not in CONTAINER_EXTS:
            return True
        return code_has_video  # superseded by the real thing
    return (size or 0) < JUNK_BYTES


def _codes_with_video(entries: list) -> set[str]:
    """Codes in this folder that already have a real playable video —
    substantial enough to clear the ad-clip bar, and fully written, so a
    half-landed transfer never condemns the container it replaces."""
    out: set[str] = set()
    for e in entries:
        if not is_video(e.name) or (e.size or 0) < JUNK_BYTES:
            continue
        if (getattr(e, "phase", "") or "") not in ("", "PHASE_TYPE_COMPLETE"):
            continue
        code = extract_jav_code(e.name)
        if code:
            out.add(code)
    return out


async def purge_series_junk_stream(
    svc, *, dry_run: bool = True
) -> AsyncIterator[dict[str, Any]]:
    """Walk 製作商/<studio>/<series> and trash the junk sitting loose in
    each series folder. Yields one ``progress`` per hit, then ``done``."""
    studio_path = next((p for k, p in all_kind_paths() if k == "studio"), "")
    if not studio_path:
        yield {"type": "done",
               "result": {"scanned": 0, "trashed": 0, "dry_run": dry_run}}
        return
    try:
        root_id = await svc.lookup_folder_id(studio_path)
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": f"解析 {studio_path} 失敗: {exc}"}
        root_id = ""
    if not root_id:
        yield {"type": "done",
               "result": {"scanned": 0, "trashed": 0, "dry_run": dry_run}}
        return

    sem = asyncio.Semaphore(_LIST_CONCURRENCY)

    async def ls(folder_id: str) -> list:
        async with sem:
            try:
                files, _partial = await svc.list_all_files(folder_id, cap=5000)
                return files
            except Exception as exc:  # noqa: BLE001
                logger.debug("series junk list %s failed: %s", folder_id, exc)
                return []

    hits: list[tuple[str, str]] = []  # (file id, display path)
    scanned = 0
    for studio in await ls(root_id):
        if studio.kind != "drive#folder":
            continue
        for series in await ls(studio.id):
            if series.kind != "drive#folder":
                continue
            entries = [f for f in await ls(series.id) if f.kind != "drive#folder"]
            playable = _codes_with_video(entries)
            for f in entries:
                scanned += 1
                code = extract_jav_code(f.name)
                if is_series_junk(
                    f.name, f.size, getattr(f, "phase", ""),
                    code_has_video=bool(code) and code in playable,
                ):
                    hits.append(
                        (f.id,
                         f"{studio_path}/{studio.name}/{series.name}/{f.name}")
                    )

    for _fid, path in hits:
        yield {"type": "progress", "action": "trash", "target": path}
    if hits and not dry_run:
        ids = [fid for fid, _ in hits]
        for i in range(0, len(ids), _TRASH_BATCH):
            try:
                await svc.trash_files(ids[i:i + _TRASH_BATCH])
            except Exception as exc:  # noqa: BLE001
                yield {"type": "error", "message": f"trash 失敗: {exc}"}
        logger.info("series junk: trashed %d file(s)", len(hits))
    yield {
        "type": "done",
        "result": {"scanned": scanned, "trashed": len(hits),
                   "dry_run": dry_run},
    }


async def purge_series_junk(svc, *, dry_run: bool = True) -> dict[str, Any]:
    """Non-streaming wrapper for the archiver loop / cron."""
    summary: dict[str, Any] = {}
    async for ev in purge_series_junk_stream(svc, dry_run=dry_run):
        if ev.get("type") == "done":
            summary = ev.get("result") or {}
    return summary
