"""Two-phase "reorganize" pipeline.

Phase 1 — **Migrate**. Walk ``AVBT/已完成/`` and move each child to its
hierarchical destination ``AVBT/<kind>/<safe_name>/<code>`` (folders) or
``AVBT/<kind>/<safe_name>/<code>.<ext>`` (bare files). Items whose code
doesn't match any tracked listing stay put.

Phase 2 — **Cleanup destinations**. For every tracked listing, walk its
target folder ``AVBT/<kind>/<safe_name>/``, group children by code, and:
  - When two or more items share a code (e.g. ``SDMM-14901/`` folder
    sitting next to ``SDMM-14901.mp4`` bare file from an earlier
    cleanup-flatten pass), keep the one with the largest video payload
    and trash the rest (PikPak's trash is recoverable for ~30 days).
  - Rename whatever's kept to its canonical name — ``<code>`` for a
    folder, ``<code>.<ext>`` for a file.

Dry-run emits the same NDJSON events but performs no mutations.

Events:
  ``start``    { total, dry_run, source_folder }
  ``progress`` { current, kind, source, action, target, reason, section, context }
                action ∈ {move, rename, dedupe, skip, error}
                section ∈ {migrate, cleanup}
                context = the kind/name path being processed (cleanup only)
  ``done``     { result: { total, moved, renamed, deduped, skipped, errors, dry_run } }
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from sqlalchemy import select

from ..config import kind_base_path, settings
from ..database import SessionLocal
from ..models import TrackedListing
from .archiver import _resolve_archive_path, _safe_code, _safe_name
from .jav_code import ext_of, extract_jav_code, is_video
from .pikpak import pikpak_service
from .pikpak_presence import presence_index


logger = logging.getLogger(__name__)


# BT releases bundle tiny ad mp4s alongside the real video. Anything well
# below 300 MB is junk; the real episode is almost always larger.
_JUNK_BYTES = 300 * 1024 * 1024


async def _item_size(item) -> int:
    """Approximate "video payload" size of a PikPak child. For a file
    use its own size; for a folder, sum sizes of contained files (peek
    one level deeper if the top level is empty of files)."""
    if item.kind != "drive#folder":
        return int(item.size or 0)
    try:
        inner = await pikpak_service.list_files(item.id, size=100)
    except Exception:  # noqa: BLE001
        return 0
    total = sum(int(c.size or 0) for c in inner if c.kind != "drive#folder")
    if total == 0:
        # Wrapper folder with only sub-folders → peek one more level.
        for sub in inner:
            if sub.kind != "drive#folder":
                continue
            try:
                deeper = await pikpak_service.list_files(sub.id, size=50)
            except Exception:  # noqa: BLE001
                continue
            total += sum(
                int(d.size or 0) for d in deeper if d.kind != "drive#folder"
            )
    return total


def _canonical_name(child, code: str) -> str:
    """Canonical leaf name for a child given its extracted code."""
    if child.kind == "drive#folder":
        return _safe_code(code) or code
    return f"{_safe_code(code) or code}{ext_of(child.name)}"


async def _phase1_migrate(
    *, dry_run: bool, idx_start: int
) -> AsyncIterator[dict]:
    """Yield events for legacy → hierarchy migration. Returns when done.

    Uses module-level counters via closure-style state by yielding event
    dicts that already contain the proper ``current`` value."""
    legacy_path = settings.pikpak_archive_folder or "AVBT/已完成"
    try:
        legacy_id = await pikpak_service.folder_id(legacy_path)
    except Exception as exc:  # noqa: BLE001
        yield {"type": "_phase1_error", "message": f"無法解析來源資料夾: {exc}"}
        return
    if not legacy_id:
        yield {"type": "_phase1_error", "message": f"找不到資料夾 {legacy_path}"}
        return

    try:
        children = await pikpak_service.list_files(legacy_id, size=500)
    except Exception as exc:  # noqa: BLE001
        yield {"type": "_phase1_error", "message": f"列出資料夾失敗: {exc}"}
        return

    yield {"type": "_phase1_total", "count": len(children), "source": legacy_path}

    target_parent_cache: dict[str, str] = {}
    siblings_cache: dict[str, set[str]] = {}

    idx = idx_start
    for child in children:
        await asyncio.sleep(0.05)
        idx += 1
        is_folder = (child.kind == "drive#folder")
        base = {
            "type": "progress",
            "current": idx,
            "kind": "folder" if is_folder else "file",
            "source": child.name,
            "section": "migrate",
            "context": legacy_path,
        }

        code = extract_jav_code(child.name)
        if not code:
            yield {**base, "action": "skip", "target": None, "reason": "no_code"}
            continue

        try:
            target_path = await _resolve_archive_path(code)
        except Exception as exc:  # noqa: BLE001
            yield {**base, "action": "error", "target": None,
                   "reason": f"resolve_failed: {exc}"}
            continue

        legacy_target = f"{legacy_path}/{_safe_code(code)}"
        if target_path == legacy_target:
            yield {**base, "action": "skip", "target": target_path,
                   "reason": "no_tracked_match"}
            continue

        if "/" not in target_path:
            yield {**base, "action": "skip", "target": target_path,
                   "reason": "bad_target"}
            continue

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
                yield {**base, "action": "skip", "target": display_target,
                       "reason": "conflict"}
                continue

            if not dry_run:
                await pikpak_service.move_files([child.id], parent_id)
                if child.name != leaf:
                    try:
                        await pikpak_service.rename_file(child.id, leaf)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "rename %s → %s failed: %s", child.name, leaf, exc
                        )
                sibling_names.add(leaf)

            yield {**base, "action": "move", "target": display_target, "reason": None}

        except Exception as exc:  # noqa: BLE001
            logger.warning("reorganize %s failed: %s", child.name, exc)
            yield {**base, "action": "error", "target": display_target,
                   "reason": str(exc)}


async def _resolve_folder_winner(
    folder, code: str, parent_id: str, *, dry_run: bool
) -> dict:
    """A code-bearing folder that survived the dedupe step.

    - If it contains ≥1 main video, flatten: keep the largest as a bare
      ``<code>.<ext>`` at the parent level, trash everything else (other
      videos, ads, wrapper).
    - If it contains zero main videos, just rename the folder to the
      canonical ``<code>`` (or skip when already there).

    Returns ``{action, target, reason}`` so the caller can emit one
    progress event.
    """
    canonical_folder = _safe_code(code) or code
    try:
        inner = await pikpak_service.list_files(folder.id, size=200)
    except Exception as exc:  # noqa: BLE001
        return {"action": "error", "target": canonical_folder, "reason": str(exc)}

    videos = [
        i for i in inner if i.kind != "drive#folder" and is_video(i.name)
    ]
    main_videos = [
        v for v in videos if v.size is None or v.size >= _JUNK_BYTES
    ]

    if not main_videos:
        # Nothing to extract — just normalise the folder name.
        if folder.name == canonical_folder:
            return {"action": "skip", "target": canonical_folder,
                    "reason": "already_clean"}
        if not dry_run:
            try:
                await pikpak_service.rename_file(folder.id, canonical_folder)
            except Exception as exc:  # noqa: BLE001
                return {"action": "error", "target": canonical_folder,
                        "reason": str(exc)}
        return {"action": "rename", "target": canonical_folder, "reason": None}

    # Pick the largest main video.
    main_videos.sort(key=lambda v: int(v.size or 0), reverse=True)
    keeper = main_videos[0]
    trash_ids = [i.id for i in inner if i.id != keeper.id]
    canonical_file = f"{canonical_folder}{ext_of(keeper.name)}"

    if not dry_run:
        try:
            await pikpak_service.move_files([keeper.id], parent_id)
            if keeper.name != canonical_file:
                try:
                    await pikpak_service.rename_file(keeper.id, canonical_file)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "rename keeper %s → %s failed: %s",
                        keeper.name, canonical_file, exc,
                    )
            if trash_ids:
                try:
                    await pikpak_service.trash_files(trash_ids)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("trash inner of %s failed: %s",
                                   folder.name, exc)
            try:
                await pikpak_service.trash_files([folder.id])
            except Exception as exc:  # noqa: BLE001
                logger.warning("trash wrapper %s failed: %s",
                               folder.name, exc)
        except Exception as exc:  # noqa: BLE001
            return {"action": "error", "target": canonical_file,
                    "reason": f"flatten failed: {exc}"}

    extras = len(trash_ids)
    reason = (
        f"取出主檔，清掉 {extras} 個垃圾/額外檔" if extras else "取出主檔"
    )
    return {"action": "flatten", "target": canonical_file, "reason": reason}


async def _phase2_cleanup_target(
    target_path: str,
    target_id: str,
    children,
    *,
    dry_run: bool,
    idx_start: int,
) -> AsyncIterator[dict]:
    """Yield events for one ``AVBT/<kind>/<name>/`` cleanup."""
    # Group children by extracted code.
    groups: dict[str, list] = {}
    no_code_items: list = []

    for c in children:
        code = extract_jav_code(c.name)
        if not code:
            no_code_items.append(c)
            continue
        groups.setdefault(code, []).append(c)

    # Pick winner per group (largest payload; ties broken by preferring
    # bare files over folders since they play directly).
    winner_ids: dict[str, str] = {}
    for code, items in groups.items():
        if len(items) == 1:
            winner_ids[code] = items[0].id
            continue
        ranked: list[tuple[int, int, object]] = []
        for it in items:
            size = await _item_size(it)
            file_bias = 0 if it.kind == "drive#folder" else 1
            ranked.append((size, file_bias, it))
        ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
        winner_ids[code] = ranked[0][2].id

    # Build the plan. plan items: (child, phase, target_name, reason, code)
    # phase ∈ {"skip", "dedupe", "rename", "winner_folder"}
    plan: list[tuple[object, str, str | None, str | None, str | None]] = []
    for c in no_code_items:
        plan.append((c, "skip", None, "no_code", None))

    for code, items in groups.items():
        for c in items:
            if c.id != winner_ids[code]:
                # Loser — trash regardless of kind.
                plan.append(
                    (c, "dedupe", _canonical_name(c, code), "duplicate", code)
                )
                continue
            # Winner.
            if c.kind == "drive#folder":
                # Defer decision: needs an inner-content peek.
                plan.append(
                    (c, "winner_folder", _canonical_name(c, code), None, code)
                )
            else:
                canonical = _canonical_name(c, code)
                if c.name == canonical:
                    plan.append(
                        (c, "skip", canonical, "already_clean", code)
                    )
                else:
                    plan.append((c, "rename", canonical, None, code))

    # Order: dedupes first (free up names), then winner_folder (may
    # flatten into a freed name), then plain renames, then skips.
    order = {"dedupe": 0, "winner_folder": 1, "rename": 2, "skip": 3}
    plan.sort(key=lambda p: order.get(p[1], 4))

    idx = idx_start
    for c, phase, target_name, reason, code in plan:
        await asyncio.sleep(0.03)
        idx += 1
        base = {
            "type": "progress",
            "current": idx,
            "kind": "folder" if c.kind == "drive#folder" else "file",
            "source": c.name,
            "section": "cleanup",
            "context": target_path,
        }
        try:
            if phase == "dedupe":
                if not dry_run:
                    await pikpak_service.trash_files([c.id])
                yield {**base, "action": "dedupe", "target": target_name,
                       "reason": reason}
            elif phase == "winner_folder":
                resolved = await _resolve_folder_winner(
                    c, code or "", target_id, dry_run=dry_run
                )
                yield {**base, **resolved}
            elif phase == "rename":
                if not dry_run:
                    await pikpak_service.rename_file(c.id, target_name)
                yield {**base, "action": "rename", "target": target_name,
                       "reason": None}
            else:
                yield {**base, "action": "skip", "target": target_name,
                       "reason": reason}
        except Exception as exc:  # noqa: BLE001
            logger.warning("cleanup %s failed: %s", c.name, exc)
            yield {**base, "action": "error", "target": target_name,
                   "reason": str(exc)}


async def reorganize_stream(*, dry_run: bool) -> AsyncIterator[dict]:
    legacy_path = settings.pikpak_archive_folder or "AVBT/已完成"

    # ---- Pre-flight: resolve totals so the progress bar has a real
    # denominator. Also lets us short-circuit if PikPak isn't reachable.
    try:
        legacy_id = await pikpak_service.folder_id(legacy_path)
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": f"無法解析來源資料夾: {exc}"}
        return

    try:
        legacy_children = (
            await pikpak_service.list_files(legacy_id, size=500)
            if legacy_id else []
        )
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": f"列出資料夾失敗: {exc}"}
        return

    # Resolve every tracked listing's destination folder. folder_id auto-
    # creates, so an empty tracked listing just yields an empty folder
    # with zero work — harmless.
    async with SessionLocal() as session:
        tracked_rows = (
            await session.execute(select(TrackedListing))
        ).scalars().all()

    cleanup_targets: list[tuple[str, str, list]] = []
    for row in tracked_rows:
        safe = _safe_name(
            row.name, fallback=_safe_name(row.id, fallback="unknown")
        )
        target_path = f"{kind_base_path(row.kind)}/{safe}"
        try:
            # Lookup-only so we don't pollute PikPak with empty folders
            # for tracked listings that have nothing downloaded yet.
            target_id = await pikpak_service.lookup_folder_id(target_path)
            if not target_id:
                continue
            children = await pikpak_service.list_files(target_id, size=500)
        except Exception as exc:  # noqa: BLE001
            logger.warning("can't list %s: %s", target_path, exc)
            continue
        if children:
            cleanup_targets.append((target_path, target_id, children))

    total = len(legacy_children) + sum(len(c) for _, _, c in cleanup_targets)

    summary = {
        "total": total,
        "moved": 0,
        "renamed": 0,
        "flattened": 0,
        "deduped": 0,
        "skipped": 0,
        "errors": 0,
        "dry_run": dry_run,
    }

    yield {
        "type": "start",
        "total": total,
        "dry_run": dry_run,
        "source_folder": legacy_path,
        "cleanup_targets": [p for p, _, _ in cleanup_targets],
    }

    idx = 0

    # ---- Phase 1: migrate ----
    async for ev in _phase1_migrate(dry_run=dry_run, idx_start=idx):
        if ev.get("type") == "_phase1_error":
            yield {"type": "error", "message": ev.get("message", "")}
            continue
        if ev.get("type") == "_phase1_total":
            continue  # already accounted for in `total`
        # Track summary counts.
        action = ev.get("action")
        if action == "move":
            summary["moved"] += 1
        elif action == "skip":
            summary["skipped"] += 1
        elif action == "error":
            summary["errors"] += 1
        idx = ev.get("current", idx)
        yield ev

    # ---- Phase 2: cleanup destinations ----
    for target_path, target_id, children in cleanup_targets:
        async for ev in _phase2_cleanup_target(
            target_path, target_id, children, dry_run=dry_run, idx_start=idx
        ):
            action = ev.get("action")
            if action == "rename":
                summary["renamed"] += 1
            elif action == "flatten":
                summary["flattened"] += 1
            elif action == "dedupe":
                summary["deduped"] += 1
            elif action == "skip":
                summary["skipped"] += 1
            elif action == "error":
                summary["errors"] += 1
            idx = ev.get("current", idx)
            yield ev

    mutated = (
        not dry_run
        and (
            summary["moved"] + summary["renamed"]
            + summary["flattened"] + summary["deduped"]
        ) > 0
    )
    if mutated:
        presence_index.invalidate()
        pikpak_service._folder_cache.clear()

    yield {"type": "done", "result": summary}
