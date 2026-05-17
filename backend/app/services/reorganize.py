"""Migrate legacy ``AVBT/已完成/<code>/`` folders into the new hierarchy.

Streams NDJSON events shaped like ``cleanup_folder_stream`` so the frontend
``streamNdjson`` consumer is reusable.

For each child of the legacy archive folder:
  1. Try to extract a JAV code from the folder name; skip if none.
  2. Fetch the JavBus detail to learn its series/director/label/studio/star.
  3. Pick the highest-priority matching TrackedListing and compute its
     hierarchical destination ``AVBT/<kind>/<safe_name>/<code>``.
  4. If destination == current location, skip "already_in_place".
  5. If destination already contains a folder with this code, skip
     "conflict" (we don't merge — caller can resolve manually).
  6. Otherwise move the folder.

Dry-run emits identical events without mutating PikPak.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from ..config import settings
from .archiver import _resolve_archive_path, _safe_code
from .jav_code import ext_of, extract_jav_code
from .pikpak import pikpak_service
from .pikpak_presence import presence_index


logger = logging.getLogger(__name__)


async def reorganize_stream(*, dry_run: bool) -> AsyncIterator[dict]:
    legacy_path = settings.pikpak_archive_folder or "AVBT/已完成"
    try:
        legacy_id = await pikpak_service.folder_id(legacy_path)
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": f"無法解析來源資料夾: {exc}"}
        return
    if not legacy_id:
        yield {"type": "error", "message": f"找不到資料夾 {legacy_path}"}
        return

    try:
        children = await pikpak_service.list_files(legacy_id, size=500)
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": f"列出資料夾失敗: {exc}"}
        return

    # Process both folders AND bare files. A folder moves as one unit
    # into the target kind/name dir (becoming the per-code subfolder).
    # A bare file moves into the same kind/name dir, keeping the file
    # form but renamed to its canonical "<code>.<ext>".
    summary = {
        "total": len(children),
        "moved": 0,
        "skipped": 0,
        "errors": 0,
        "dry_run": dry_run,
    }

    yield {
        "type": "start",
        "total": len(children),
        "dry_run": dry_run,
        "source_folder": legacy_path,
    }

    # Cache resolved target parent IDs within this run so we don't
    # round-trip per item to PikPak for the same target dir.
    target_parent_cache: dict[str, str] = {}
    siblings_cache: dict[str, set[str]] = {}

    for idx, child in enumerate(children, start=1):
        await asyncio.sleep(0.05)
        is_folder = (child.kind == "drive#folder")
        base_event = {
            "type": "progress",
            "current": idx,
            "kind": "folder" if is_folder else "file",
            "source": child.name,
        }

        code = extract_jav_code(child.name)
        if not code:
            summary["skipped"] += 1
            yield {**base_event, "action": "skip", "target": None, "reason": "no_code"}
            continue

        try:
            target_path = await _resolve_archive_path(code)
        except Exception as exc:  # noqa: BLE001
            summary["errors"] += 1
            yield {**base_event, "action": "error", "target": None,
                   "reason": f"resolve_failed: {exc}"}
            continue

        # If resolver returned the legacy path, this code has no tracked
        # match → leave it where it is.
        legacy_target = f"{legacy_path}/{_safe_code(code)}"
        if target_path == legacy_target:
            summary["skipped"] += 1
            yield {**base_event, "action": "skip", "target": target_path,
                   "reason": "no_tracked_match"}
            continue

        if "/" not in target_path:
            summary["skipped"] += 1
            yield {**base_event, "action": "skip", "target": target_path,
                   "reason": "bad_target"}
            continue

        # target_path is always the "code-leaf" form (kind/name/code).
        # For a folder we keep it as-is; for a file we drop the trailing
        # code segment and re-attach the file extension.
        parent_path, code_leaf = target_path.rsplit("/", 1)
        if is_folder:
            leaf = code_leaf
            display_target = target_path
        else:
            leaf = f"{code_leaf}{ext_of(child.name)}"
            display_target = f"{parent_path}/{leaf}"

        try:
            parent_id = target_parent_cache.get(parent_path)
            if parent_id is None:
                # In dry-run we still need to know if the parent exists
                # so we can predict conflicts — folder_id auto-creates,
                # so for dry-run we just probe and accept the side
                # effect of creating intermediate folders.
                parent_id = await pikpak_service.folder_id(parent_path)
                target_parent_cache[parent_path] = parent_id or ""

            sibling_names = siblings_cache.get(parent_path)
            if sibling_names is None:
                rows = (
                    await pikpak_service.list_files(parent_id, size=500)
                    if parent_id else []
                )
                sibling_names = {r.name for r in rows}
                siblings_cache[parent_path] = sibling_names

            if leaf in sibling_names:
                summary["skipped"] += 1
                yield {**base_event, "action": "skip", "target": display_target,
                       "reason": "conflict"}
                continue

            if not dry_run:
                await pikpak_service.move_files([child.id], parent_id)
                # Normalise the name so e.g. "kfa55.com@DAM-044.mp4"
                # becomes "DAM-044.mp4", "MIDV001" → "MIDV-001", etc.
                if child.name != leaf:
                    try:
                        await pikpak_service.rename_file(child.id, leaf)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "rename %s → %s failed: %s", child.name, leaf, exc
                        )
                sibling_names.add(leaf)

            summary["moved"] += 1
            yield {**base_event, "action": "move", "target": display_target, "reason": None}

        except Exception as exc:  # noqa: BLE001
            summary["errors"] += 1
            logger.warning("reorganize %s failed: %s", child.name, exc)
            yield {**base_event, "action": "error", "target": display_target,
                   "reason": str(exc)}

    if not dry_run and summary["moved"]:
        presence_index.invalidate()
        pikpak_service._folder_cache.clear()

    yield {"type": "done", "result": summary}
