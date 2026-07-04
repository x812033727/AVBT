"""How many video files does a work actually have on PikPak?

Answers "這部是分集還是單一影片" with real file counts, for two lookup
keys:

- ``count_for_file_id`` — a live offline task's content id. Only valid
  BEFORE archiving: the archiver's flatten pass moves the videos out of
  the wrapper folder and trashes the wrapper, so old file_ids dangle.
- ``count_for_code`` — a JAV code, resolved through the presence index
  to the archived folder(s). Falls back to the newest offline-task
  file_id when the presence index has no path for the code.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from ..database import SessionLocal
from ..models import OfflineTaskLog
from .jav_code import is_video, normalize_code
from .pikpak import pikpak_service
from .pikpak_presence import presence_index

logger = logging.getLogger(__name__)

_MAX_NAMES = 20  # cap the example names in a response
_MAX_PATHS = 3   # presence may know several copies of one work
_MAX_SUBFOLDERS = 5  # nested-level scan cap inside one task folder


def summarize_children(children) -> dict:
    """Split a folder listing into video/file counts plus subfolder ids
    (for the one-level recursion in count_for_file_id)."""
    video_count = 0
    total_files = 0
    video_names: list[str] = []
    subfolder_ids: list[str] = []
    for child in children:
        if getattr(child, "kind", "") == "drive#folder":
            if child.id:
                subfolder_ids.append(child.id)
            continue
        total_files += 1
        if is_video(child.name):
            video_count += 1
            if len(video_names) < _MAX_NAMES:
                video_names.append(child.name)
    return {
        "video_count": video_count,
        "video_names": video_names,
        "total_files": total_files,
        "subfolder_ids": subfolder_ids,
    }


async def count_for_file_id(file_id: str) -> dict:
    """Count videos inside an offline task's content (folder or file)."""
    if not file_id:
        return {"ok": False, "error": "缺少 file_id"}
    children, partial = await pikpak_service.list_all_files(file_id)
    if children:
        summary = summarize_children(children)
        # BT wrappers occasionally nest one level (folder-in-folder);
        # deeper nesting is rare enough to ignore.
        for sub_id in summary["subfolder_ids"][:_MAX_SUBFOLDERS]:
            try:
                sub_children, sub_partial = await pikpak_service.list_all_files(sub_id)
            except Exception as exc:  # noqa: BLE001 — one bad subfolder shouldn't kill the count
                logger.debug("video-count sublist %s failed: %s", sub_id, exc)
                continue
            sub = summarize_children(sub_children)
            summary["video_count"] += sub["video_count"]
            summary["total_files"] += sub["total_files"]
            room = _MAX_NAMES - len(summary["video_names"])
            if room > 0:
                summary["video_names"].extend(sub["video_names"][:room])
            partial = partial or sub_partial
        return {
            "ok": True,
            "video_count": summary["video_count"],
            "video_names": summary["video_names"],
            "partial": bool(partial),
            "source": "task",
        }

    # Empty listing: either a bare file, a still-downloading folder, or
    # a dangling id (wrapper trashed after archive).
    try:
        meta = await pikpak_service.file_meta(file_id)
    except Exception as exc:  # noqa: BLE001 — dangling id surfaces as not-found
        logger.debug("video-count file_meta %s failed: %s", file_id, exc)
        meta = {}
    if not meta or not meta.get("name"):
        return {"ok": False, "error": "找不到檔案(可能已歸檔搬移或刪除)"}
    if meta.get("kind") == "drive#folder":
        # Existing but empty folder → nothing downloaded yet.
        return {"ok": True, "video_count": 0, "video_names": [], "source": "task"}
    n = 1 if is_video(meta["name"]) else 0
    return {
        "ok": True,
        "video_count": n,
        "video_names": [meta["name"]] if n else [],
        "source": "task",
    }


async def _latest_task_file_id(code: str) -> str:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(OfflineTaskLog.file_id)
                .where(OfflineTaskLog.code == code, OfflineTaskLog.file_id != "")
                .order_by(OfflineTaskLog.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return row or ""


async def count_for_code(code: str) -> dict:
    """Count videos for a code via its archived location(s)."""
    code = normalize_code(code) or (code or "").strip().upper()
    if not code:
        return {"ok": False, "error": "無效番號"}

    await presence_index.get()  # lazily (re)build within TTL
    paths = presence_index.paths_for(code)[:_MAX_PATHS]

    entries: list[dict] = []
    best_names: list[str] = []
    best_count = -1
    any_partial = False
    for path in paths:
        leaf = path.rsplit("/", 1)[-1]
        if is_video(leaf):
            entries.append({"path": path, "video_count": 1})
            if 1 > best_count:
                best_count, best_names = 1, [leaf]
            continue
        try:
            folder_id = await pikpak_service.lookup_folder_id(path)
        except Exception as exc:  # noqa: BLE001 — stale presence path
            logger.debug("video-count lookup %s failed: %s", path, exc)
            continue
        if not folder_id:
            continue
        try:
            children, partial = await pikpak_service.list_all_files(folder_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("video-count list %s failed: %s", path, exc)
            continue
        summary = summarize_children(children)
        any_partial = any_partial or bool(partial)
        entries.append({"path": path, "video_count": summary["video_count"]})
        if summary["video_count"] > best_count:
            best_count = summary["video_count"]
            best_names = summary["video_names"]

    if entries:
        return {
            "ok": True,
            # Paths are duplicate homes of the SAME work — report the
            # richest copy, not the sum.
            "video_count": max(best_count, 0),
            "video_names": best_names,
            "entries": entries,
            "partial": any_partial,
            "source": "presence",
        }

    # Presence knows nothing (not archived / index stale) — try the most
    # recent offline task for the code.
    file_id = await _latest_task_file_id(code)
    if not file_id:
        return {"ok": False, "error": "PikPak 上找不到此番號"}
    result = await count_for_file_id(file_id)
    if not result.get("ok"):
        return {"ok": False, "error": "PikPak 上找不到此番號(任務檔案已搬移)"}
    return result
