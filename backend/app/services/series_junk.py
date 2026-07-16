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
  playable video for the same code turns up **anywhere under 製作商**,
  which is what the container swap arranges. Then it is redundant and
  gets trashed: that is the tail end of the swap. Anywhere, not beside
  it: series folder names drift (``新人NO.1 STYLE`` vs ``新人NO.1STYLE``)
  and the replacement lands wherever the archiver resolves today.
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
# How small a replacement may be, relative to the container it retires.
# A disc image carries the film uncompressed, so an equal-quality mp4 is
# legitimately a fraction of its size (live: IPTD-770 4.3GB → 2.3GB, 53%;
# ATOM-121 8.1GB → 2.6GB, 32%). Far below that is not a re-encode, it is
# a worse rip: SNIS-494's Blu-ray image is 23.85GB and the magnet the
# swap found was a 2.0GB avi — 8%. Retiring the only high-quality copy
# for that is the 淨降級 the backfill's own "magnet must be ≥1.8× what we
# have" rule exists to prevent; this is the same rule facing the other
# way. Below the bar both are kept and a human decides.
MIN_REPLACEMENT_FRACTION = 0.25
_LIST_CONCURRENCY = 5
_TRASH_BATCH = 50


def is_series_junk(
    name: str,
    size: int | None,
    phase: str = "",
    *,
    video_bytes: int = 0,
) -> bool:
    """Whether a file sitting directly in a 系列 folder is BT junk.

    ``video_bytes`` is the biggest playable video for this file's code
    anywhere in the archive (0 = none). It only ever promotes a container
    to junk, and only when it is a credible replacement rather than a
    downgrade.
    """
    if phase not in ("", "PHASE_TYPE_COMPLETE"):
        return False  # still being written — hands off (#129)
    if not is_video(name):
        if ext_of(name) not in CONTAINER_EXTS:
            return True
        if not video_bytes:
            return False  # the only copy of the work
        return video_bytes >= (size or 0) * MIN_REPLACEMENT_FRACTION
    return (size or 0) < JUNK_BYTES


def _codes_with_video(entries: list) -> dict[str, int]:
    """Code → biggest playable video for it anywhere in the scanned tree.
    Substantial enough to clear the ad-clip bar, and fully written, so a
    half-landed transfer never condemns the container it replaces."""
    out: dict[str, int] = {}
    for e in entries:
        if not is_video(e.name) or (e.size or 0) < JUNK_BYTES:
            continue
        if (getattr(e, "phase", "") or "") not in ("", "PHASE_TYPE_COMPLETE"):
            continue
        code = extract_jav_code(e.name)
        if code:
            out[code] = max(out.get(code, 0), e.size or 0)
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

    # Collect the whole tree first. A container only retires once a real
    # video for its code exists, and that video does NOT reliably land
    # beside it: series folder names drift (live: SNIS-494.iso sits in
    # "新人NO.1 STYLE" while its swapped-in .avi landed in "新人NO.1STYLE"),
    # so a per-folder answer would keep every drifted container forever.
    # The walk already reads every file — decide against all of them.
    collected: list[tuple[Any, str]] = []  # (entry, display path)
    for studio in await ls(root_id):
        if studio.kind != "drive#folder":
            continue
        for series in await ls(studio.id):
            if series.kind != "drive#folder":
                continue
            for f in await ls(series.id):
                if f.kind == "drive#folder":
                    continue
                collected.append(
                    (f, f"{studio_path}/{studio.name}/{series.name}/{f.name}"))

    playable = _codes_with_video([e for e, _p in collected])
    hits: list[tuple[str, str]] = []  # (file id, display path)
    scanned = len(collected)
    for f, path in collected:
        code = extract_jav_code(f.name)
        if is_series_junk(
            f.name, f.size, getattr(f, "phase", ""),
            video_bytes=playable.get(code, 0) if code else 0,
        ):
            hits.append((f.id, path))
        elif code and ext_of(f.name) in CONTAINER_EXTS and playable.get(code):
            # Kept on purpose — say so, or it looks like the sweep missed it.
            logger.info(
                "series junk: %s kept, replacement is only %.0f%% of it",
                path, 100 * playable[code] / (f.size or 1))

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
