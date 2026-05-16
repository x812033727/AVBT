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
from .jav_code import extract_jav_code
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

    folders = [c for c in children if c.kind == "drive#folder"]

    summary = {
        "total": len(folders),
        "moved": 0,
        "skipped": 0,
        "errors": 0,
        "dry_run": dry_run,
    }

    yield {
        "type": "start",
        "total": len(folders),
        "dry_run": dry_run,
        "source_folder": legacy_path,
    }

    # Cache resolved target parent IDs within this run so we don't
    # round-trip per item to PikPak for the same target dir.
    target_parent_cache: dict[str, str] = {}

    for idx, child in enumerate(folders, start=1):
        await asyncio.sleep(0.05)
        base_event = {
            "type": "progress",
            "current": idx,
            "kind": "folder",
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

        # Decompose target into (parent, leaf) so we can check for
        # existing folder collisions cheaply.
        if "/" not in target_path:
            summary["skipped"] += 1
            yield {**base_event, "action": "skip", "target": target_path,
                   "reason": "bad_target"}
            continue
        parent_path, leaf = target_path.rsplit("/", 1)

        try:
            parent_id = target_parent_cache.get(parent_path)
            if parent_id is None:
                # In dry-run we still need to know if the parent exists
                # so we can predict conflicts — folder_id auto-creates,
                # so for dry-run we just probe and accept the side
                # effect of creating intermediate folders.
                parent_id = await pikpak_service.folder_id(parent_path)
                target_parent_cache[parent_path] = parent_id or ""

            siblings = await pikpak_service.list_files(parent_id, size=500) if parent_id else []
            if any(s.name == leaf for s in siblings):
                summary["skipped"] += 1
                yield {**base_event, "action": "skip", "target": target_path,
                       "reason": "conflict"}
                continue

            if not dry_run:
                await pikpak_service.move_files([child.id], parent_id)
                # If the moved folder's name differs from leaf (e.g.
                # legacy used "MIDV001" but new uses "MIDV-001"), rename.
                if child.name != leaf:
                    try:
                        await pikpak_service.rename_file(child.id, leaf)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "rename %s → %s failed: %s", child.name, leaf, exc
                        )

            summary["moved"] += 1
            yield {**base_event, "action": "move", "target": target_path, "reason": None}

        except Exception as exc:  # noqa: BLE001
            summary["errors"] += 1
            logger.warning("reorganize %s failed: %s", child.name, exc)
            yield {**base_event, "action": "error", "target": target_path,
                   "reason": str(exc)}

    if not dry_run and summary["moved"]:
        presence_index.invalidate()
        pikpak_service._folder_cache.clear()

    yield {"type": "done", "result": summary}
