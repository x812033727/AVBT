"""How many video files does a work actually have on PikPak?

Answers "這部是分集還是單一影片" with real file counts, for two lookup
keys:

- ``count_for_file_id`` — a live offline task's content id. Only valid
  BEFORE archiving: the archiver's flatten pass moves the videos out of
  the wrapper folder and trashes the wrapper, so old file_ids dangle.
- ``count_for_code`` — a JAV code, resolved through the presence index
  to the archived folder(s). Falls back to the newest offline-task
  file_id when the presence index has no path for the code.
- ``count_for_code_pcloud`` — a JAV code on the pCloud side. There is
  no pCloud presence index, and transfer destination folders mix many
  works, so this counts DONE rows in the pcloud_transfer table instead
  (one row = one file that reached pCloud). DB-only — may overcount if
  files were later deleted on pCloud, hence source="transfer".
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from ..database import SessionLocal
from ..models import OfflineTaskLog, PCloudTransfer
from .jav_code import extract_jav_code, is_video, normalize_code
from .pikpak import pikpak_service
from .pikpak_presence import presence_index

logger = logging.getLogger(__name__)

_MAX_NAMES = 20  # cap the example names in a response
_MAX_PATHS = 3   # presence may know several copies of one work
_MAX_SUBFOLDERS = 5  # nested-level scan cap inside one task folder


def summarize_children(children) -> dict:
    """Split a folder listing into video/file counts plus subfolder ids
    (for the one-level recursion in count_for_file_id). ``video_files``
    keeps the ids alongside the names so playback lookups can reuse the
    same listing; counting callers just ignore it."""
    video_count = 0
    total_files = 0
    video_names: list[str] = []
    video_files: list[dict] = []
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
                video_files.append(
                    {
                        "id": child.id,
                        "name": child.name,
                        "size": getattr(child, "size", None),
                    }
                )
    return {
        "video_count": video_count,
        "video_names": video_names,
        "video_files": video_files,
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
    # Loose video files grouped by their parent folder: the sweep's
    # flattened layout puts CODE_1.mp4 / CODE_2.mp4 straight into the
    # 系列 folder, and those are PARTS of one work (they sum) — only
    # copies living in *different* folders are duplicate homes that
    # compete via max() below.
    loose_by_parent: dict[str, list[str]] = {}
    folder_paths: list[str] = []
    for path in paths:
        leaf = path.rsplit("/", 1)[-1]
        if is_video(leaf):
            parent = path.rsplit("/", 1)[0]
            loose_by_parent.setdefault(parent, []).append(leaf)
        else:
            folder_paths.append(path)
    for parent, leaves in loose_by_parent.items():
        leaves = sorted(leaves)
        entry_path = f"{parent}/{leaves[0]}" if len(leaves) == 1 else parent
        entries.append({"path": entry_path, "video_count": len(leaves)})
        if len(leaves) > best_count:
            best_count, best_names = len(leaves), leaves
    for path in folder_paths:
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


async def files_for_code(code: str) -> dict:
    """Resolve a code to its playable PikPak video files: [{id, name,
    size, path}]. Same traversal as :func:`count_for_code` (presence
    paths first — archived task file_ids dangle), but keeps the file
    ids so the frontend can ask ``/api/pikpak/files/{id}/url`` for a
    streaming link."""
    code = normalize_code(code) or (code or "").strip().upper()
    if not code:
        return {"ok": False, "error": "無效番號"}

    await presence_index.get()
    paths = presence_index.paths_for(code)[:_MAX_PATHS]

    files: list[dict] = []
    seen_ids: set[str] = set()
    any_partial = False

    def _add(entry: dict, path: str) -> None:
        if not entry.get("id") or entry["id"] in seen_ids:
            return
        seen_ids.add(entry["id"])
        files.append({**entry, "path": path})

    for path in paths:
        parent, _, leaf = path.rpartition("/")
        try:
            if is_video(leaf):
                # Presence recorded a bare video file. Paths carry no
                # ids, so list the parent folder and match by name.
                folder_id = await pikpak_service.lookup_folder_id(parent)
                if not folder_id:
                    continue
                children, partial = await pikpak_service.list_all_files(folder_id)
                any_partial = any_partial or bool(partial)
                for child in children:
                    if (
                        getattr(child, "kind", "") != "drive#folder"
                        and child.name == leaf
                    ):
                        _add(
                            {
                                "id": child.id,
                                "name": child.name,
                                "size": getattr(child, "size", None),
                            },
                            path,
                        )
                continue
            folder_id = await pikpak_service.lookup_folder_id(path)
            if not folder_id:
                continue
            children, partial = await pikpak_service.list_all_files(folder_id)
            any_partial = any_partial or bool(partial)
            for entry in summarize_children(children)["video_files"]:
                _add(entry, f"{path}/{entry['name']}")
        except Exception as exc:  # noqa: BLE001 — one stale path shouldn't kill the lookup
            logger.debug("files-for-code %s via %s failed: %s", code, path, exc)
            continue

    if files:
        # Multi-part sets arrive in presence-path order (often _2 before
        # _1) — sort by name so the play list reads 部1, 部2, …
        files.sort(key=lambda f: f.get("name", ""))
        return {
            "ok": True,
            "code": code,
            "files": files,
            "partial": any_partial,
            "source": "presence",
        }

    # Presence knows nothing — fall back to the newest offline task.
    file_id = await _latest_task_file_id(code)
    if not file_id:
        return {"ok": False, "error": "PikPak 上找不到此番號"}
    try:
        children, partial = await pikpak_service.list_all_files(file_id)
    except Exception as exc:  # noqa: BLE001 — dangling id surfaces as not-found
        logger.debug("files-for-code task listing %s failed: %s", file_id, exc)
        children, partial = [], False
    if children:
        for entry in summarize_children(children)["video_files"]:
            _add(entry, entry["name"])
        if files:
            return {
                "ok": True,
                "code": code,
                "files": files,
                "partial": bool(partial),
                "source": "task",
            }
        return {"ok": False, "error": "PikPak 上找不到此番號"}
    # Empty listing: maybe the task content is a single bare file.
    try:
        meta = await pikpak_service.file_meta(file_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("files-for-code file_meta %s failed: %s", file_id, exc)
        meta = {}
    if (
        meta
        and meta.get("name")
        and meta.get("kind") != "drive#folder"
        and is_video(meta["name"])
    ):
        return {
            "ok": True,
            "code": code,
            "files": [
                {
                    "id": file_id,
                    "name": meta["name"],
                    "size": meta.get("size"),
                    "path": meta["name"],
                }
            ],
            "partial": False,
            "source": "task",
        }
    return {"ok": False, "error": "PikPak 上找不到此番號(任務檔案已搬移)"}


async def count_for_code_pcloud(code: str) -> dict:
    """Count videos of a code that were transferred to pCloud.

    Based purely on ``pcloud_transfer`` DONE rows — the destination
    folders mix many works, so listing them can't isolate one code.
    A later manual delete on pCloud isn't visible here."""
    code = normalize_code(code) or (code or "").strip().upper()
    if not code:
        return {"ok": False, "error": "無效番號"}

    # Coarse SQL prefilter on the label part, exact match in Python via
    # extract_jav_code (handles prefixes / squished / variant forms).
    label = code.split("-", 1)[0]
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    PCloudTransfer.pikpak_name,
                    PCloudTransfer.pikpak_file_id,
                    PCloudTransfer.pcloud_file_id,
                    PCloudTransfer.pcloud_folder_path,
                )
                .where(
                    PCloudTransfer.status == "done",
                    PCloudTransfer.pikpak_name.like(f"%{label}%"),
                )
                .order_by(PCloudTransfer.finished_at.desc())
            )
        ).all()

    seen: set[str] = set()
    video_names: list[str] = []
    per_folder: dict[str, int] = {}
    for name, pikpak_file_id, pcloud_file_id, folder_path in rows:
        if not is_video(name or ""):
            continue
        if extract_jav_code(name or "") != code:
            continue
        # Retried transfers create duplicate rows for the same file.
        dedupe_key = str(pcloud_file_id or "") or f"pk:{pikpak_file_id}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        if len(video_names) < _MAX_NAMES:
            video_names.append(name)
        per_folder[folder_path or ""] = per_folder.get(folder_path or "", 0) + 1

    if not seen:
        return {"ok": False, "error": "尚未轉存到 pCloud"}
    return {
        "ok": True,
        "video_count": len(seen),
        "video_names": video_names,
        "entries": [
            {"path": path, "video_count": n} for path, n in sorted(per_folder.items())
        ],
        "source": "transfer",
    }
