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
import re
from typing import AsyncIterator

from sqlalchemy import select

from ..config import all_kind_paths, kind_base_path, settings
from ..database import SessionLocal
from ..models import TrackedListing
from .archiver import _resolve_archive_path, _safe_code, _safe_name
from .jav_code import KIND_LABELS_CH, ext_of, extract_jav_code, is_video
from .pikpak import (
    _build_video_rename_plan,
    _uniquify_target,
    pikpak_service,
)
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


def _phase1_file_leaf(original_name: str, code_leaf: str, ext: str) -> str:
    """Pick a leaf name for a file being phase-1 moved/renamed.

    Default ``<code_leaf><ext>``, but if the source already ends in
    ``<code><variant?>_N`` (a multipart marker — three real episodes,
    not a resolution dup), preserve the ``_N``. Otherwise three parts
    sitting in AVBT root all collapse onto ``<code><ext>`` and the
    later ones end up with ``(2)`` / ``(3)`` collision suffixes, only
    for a subsequent reorganize phase-2 pass to renumber them back."""
    stem = (
        original_name[:-len(ext)]
        if ext and original_name.endswith(ext)
        else original_name
    )
    # Anchor on the code (with optional variant letter) so prefixed
    # forms like ``hhd800.com@SOE-462_1`` still get their ``_N`` kept.
    tail_re = re.compile(
        rf"(?:^|[^A-Z0-9]){re.escape(code_leaf)}[A-Z]?_(\d+)$",
        re.IGNORECASE,
    )
    m = tail_re.search(stem)
    if m:
        return f"{code_leaf}_{m.group(1)}{ext}"
    return f"{code_leaf}{ext}"


async def _phase1_migrate_from(
    source_path: str,
    *,
    dry_run: bool,
    idx_start: int,
    skip_names: frozenset[str] = frozenset(),
) -> AsyncIterator[dict]:
    """Walk ``source_path`` and migrate each child to its kind/name
    target. Children whose ``name`` is in ``skip_names`` are left alone
    (used at the AVBT root to skip the kind dirs themselves and the
    legacy ``已完成`` dir)."""
    try:
        source_id = await pikpak_service.folder_id(source_path)
    except Exception as exc:  # noqa: BLE001
        yield {"type": "_phase1_error", "message": f"無法解析 {source_path}: {exc}"}
        return
    if not source_id:
        yield {"type": "_phase1_error", "message": f"找不到資料夾 {source_path}"}
        return

    try:
        children_all = await pikpak_service.list_files(source_id, size=500)
    except Exception as exc:  # noqa: BLE001
        yield {"type": "_phase1_error", "message": f"列出 {source_path} 失敗: {exc}"}
        return

    children = [c for c in children_all if c.name not in skip_names]
    yield {"type": "_phase1_total", "count": len(children), "source": source_path}

    legacy_path = settings.pikpak_archive_folder or "AVBT/已完成"
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
            "context": source_path,
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

        # If the resolved target equals the source-as-archive fallback
        # AND we're already processing that source, nothing to do.
        legacy_target = f"{legacy_path}/{_safe_code(code)}"
        if source_path == legacy_path and target_path == legacy_target:
            yield {**base, "action": "skip", "target": target_path,
                   "reason": "no_tracked_match"}
            continue

        if "/" not in target_path:
            yield {**base, "action": "skip", "target": target_path,
                   "reason": "bad_target"}
            continue

        parent_path, code_leaf = target_path.rsplit("/", 1)

        # When the child is already a direct child of the resolved
        # target's parent (e.g. ``MFCW-054/`` already in AVBT root and
        # target resolves to ``AVBT/已完成/MFCW-054``), only rename in
        # place if needed; don't issue a no-op move.
        if parent_path == source_path:
            if is_folder:
                leaf = _phase1_file_leaf(child.name, code_leaf, "")
                display_target = f"{parent_path}/{leaf}"
            else:
                leaf = _phase1_file_leaf(
                    child.name, code_leaf, ext_of(child.name)
                )
                display_target = f"{parent_path}/{leaf}"
            if leaf == child.name:
                yield {**base, "action": "skip", "target": display_target,
                       "reason": "already_in_place"}
                continue
            try:
                if not dry_run:
                    await pikpak_service.rename_file(child.id, leaf)
                yield {**base, "action": "rename", "target": display_target,
                       "reason": None}
            except Exception as exc:  # noqa: BLE001
                yield {**base, "action": "error", "target": display_target,
                       "reason": str(exc)}
            continue

        if is_folder:
            leaf = _phase1_file_leaf(child.name, code_leaf, "")
            display_target = f"{parent_path}/{leaf}"
        else:
            leaf = _phase1_file_leaf(
                child.name, code_leaf, ext_of(child.name)
            )
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

            # On collision, deduplicate with " (N)" rather than skipping.
            # Phase 2 cleanup will resolve real winners + apply _N
            # multipart naming after all moves land.
            final_leaf = _uniquify_target(leaf, sibling_names)
            if final_leaf != leaf:
                display_target = f"{parent_path}/{final_leaf}"

            if not dry_run:
                await pikpak_service.move_files([child.id], parent_id)
                if child.name != final_leaf:
                    try:
                        await pikpak_service.rename_file(child.id, final_leaf)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "rename %s → %s failed: %s", child.name, final_leaf, exc
                        )
                sibling_names.add(final_leaf)

            yield {**base, "action": "move", "target": display_target, "reason": None}

        except Exception as exc:  # noqa: BLE001
            logger.warning("reorganize %s failed: %s", child.name, exc)
            yield {**base, "action": "error", "target": display_target,
                   "reason": str(exc)}


async def _phase1_migrate(
    *, dry_run: bool, idx_start: int
) -> AsyncIterator[dict]:
    """Walk the legacy ``AVBT/已完成`` folder (back-compat wrapper)."""
    legacy_path = settings.pikpak_archive_folder or "AVBT/已完成"
    async for ev in _phase1_migrate_from(
        legacy_path, dry_run=dry_run, idx_start=idx_start
    ):
        yield ev


def _avbt_root_skip_names() -> frozenset[str]:
    """Names we leave alone when sweeping the AVBT root: every kind dir
    that the archiver owns (per the live config, plus the English-name
    fallbacks for legacy installs) and the ``已完成`` archive dir."""
    skip: set[str] = set(KIND_LABELS_CH.keys())  # legacy English names
    root_clean = (settings.pikpak_download_folder or "AVBT").strip().strip("/")
    for _kind, path in all_kind_paths():
        path_clean = path.strip().strip("/")
        # Only direct children of the root count — custom configs that
        # point a kind dir at a nested or sibling tree don't pollute
        # AVBT root, so nothing to skip there.
        if path_clean.startswith(root_clean + "/"):
            leaf = path_clean[len(root_clean) + 1:]
            if leaf and "/" not in leaf:
                skip.add(leaf)
    legacy_name = (settings.pikpak_archive_folder or "AVBT/已完成").rsplit(
        "/", 1
    )[-1]
    skip.add(legacy_name)
    return frozenset(skip)


async def _phase1_migrate_root(
    *, dry_run: bool, idx_start: int
) -> AsyncIterator[dict]:
    """Pick up files/folders that landed at the AVBT root (manual
    uploads, magnet drops outside the app, leftovers from past tools)
    and route them into the kind/name hierarchy too."""
    root = settings.pikpak_download_folder or "AVBT"
    async for ev in _phase1_migrate_from(
        root,
        dry_run=dry_run,
        idx_start=idx_start,
        skip_names=_avbt_root_skip_names(),
    ):
        yield ev


async def _resolve_folder_winner(
    folder, code: str, parent_id: str, *, dry_run: bool
) -> dict:
    """A code-bearing folder that survived the dedupe step.

    Try hard to extract a video and trash the wrapper, in this order:
      1. Largest direct video ≥ ``_JUNK_BYTES`` (the original rule).
      2. Largest direct video of *any* size (handles small / sample-only
         downloads).
      3. Largest video found one level deeper inside a nested folder
         (handles torrent-name-dir wrappers like
         ``MIUM-1104/<torrent name>/MIUM-1104.mp4``).
      4. Truly nothing useful → either trash an empty wrapper or just
         rename it to the canonical code.

    Returns ``{action, target, reason}`` so the caller can emit one
    progress event.
    """
    canonical_folder = _safe_code(code) or code
    try:
        inner = await pikpak_service.list_files(folder.id, size=200)
    except Exception as exc:  # noqa: BLE001
        return {"action": "error", "target": canonical_folder, "reason": str(exc)}

    direct_videos = [
        i for i in inner if i.kind != "drive#folder" and is_video(i.name)
    ]
    main_videos = [
        v for v in direct_videos if v.size is None or v.size >= _JUNK_BYTES
    ]

    # Steps 2 + 3: if no "main" video, relax — fall back to any direct
    # video, then to any video one level deeper. We still flatten in
    # that case so wrappers like ``MIUM-1104/<torrent dir>/<file>``
    # collapse correctly.
    if not main_videos and direct_videos:
        main_videos = direct_videos

    if not main_videos:
        nested = [i for i in inner if i.kind == "drive#folder"]
        if nested:
            try:
                nested_lists = await asyncio.gather(
                    *[
                        pikpak_service.list_files(n.id, size=200)
                        for n in nested
                    ],
                    return_exceptions=True,
                )
            except Exception:  # noqa: BLE001
                nested_lists = []
            deep_videos: list = []
            for items in nested_lists:
                if isinstance(items, Exception):
                    continue
                for it in items:
                    if it.kind != "drive#folder" and is_video(it.name):
                        deep_videos.append(it)
            if deep_videos:
                main_videos = deep_videos

    if not main_videos:
        # Genuinely nothing to extract.
        if not inner:
            # Stray empty wrapper — trash and report as flatten so the
            # user sees their orphaned folder is gone.
            if not dry_run:
                try:
                    await pikpak_service.trash_files([folder.id])
                except Exception as exc:  # noqa: BLE001
                    return {"action": "error", "target": canonical_folder,
                            "reason": str(exc)}
            return {"action": "flatten", "target": canonical_folder,
                    "reason": "空資料夾，已刪除"}
        if folder.name == canonical_folder:
            return {"action": "skip", "target": canonical_folder,
                    "reason": "already_clean (內部無影片)"}
        if not dry_run:
            try:
                await pikpak_service.rename_file(folder.id, canonical_folder)
            except Exception as exc:  # noqa: BLE001
                return {"action": "error", "target": canonical_folder,
                        "reason": str(exc)}
        return {"action": "rename", "target": canonical_folder, "reason": None}

    # Pick the largest video at whatever depth we found one.
    main_videos.sort(key=lambda v: int(v.size or 0), reverse=True)
    keeper = main_videos[0]
    # Anything else among the wrapper's *direct* children gets trashed
    # individually; the nested folder (if keeper came from inside one)
    # gets cleared by trashing the wrapper at the end.
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
    PART_MIN = 500 * 1024 * 1024

    # First: detect real multi-part groups (2+ substantial videos that
    # share a canonical). Those get _N naming and are EXCLUDED from
    # the winner-based dedup below — otherwise we'd trash the smaller
    # episodes thinking they were resolution duplicates.
    multipart_plan, multipart_members = _build_video_rename_plan(
        children, PART_MIN, is_video
    )
    handled_ids: set[str] = set()
    idx = idx_start
    for c in children:
        if c.name not in multipart_members:
            continue
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
            if c.name in multipart_plan:
                new_name = multipart_plan[c.name]
                if not dry_run:
                    await pikpak_service.rename_file(c.id, new_name)
                yield {**base, "action": "rename", "target": new_name,
                       "reason": "多分集"}
            else:
                yield {**base, "action": "skip", "target": c.name,
                       "reason": "already_clean"}
            handled_ids.add(c.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("multipart rename %s failed: %s", c.name, exc)
            yield {**base, "action": "error", "target": None,
                   "reason": str(exc)}

    # Group remaining children by extracted code for the winner-pick
    # / canonical-name pass.
    remaining = [c for c in children if c.id not in handled_ids]
    groups: dict[str, list] = {}
    no_code_items: list = []

    for c in remaining:
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

    # ``idx`` is already advanced from the multipart pre-pass above;
    # don't reset it or the progress bar will rewind.
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
    root_path = settings.pikpak_download_folder or "AVBT"

    # ---- Pre-flight: resolve totals so the progress bar has a real
    # denominator. Also lets us short-circuit if PikPak isn't reachable.
    try:
        root_id = await pikpak_service.folder_id(root_path)
        legacy_id = await pikpak_service.folder_id(legacy_path)
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": f"無法解析來源資料夾: {exc}"}
        return

    # Sources for migration:
    #  - AVBT root direct children (skipping the kind dirs and legacy)
    #  - the legacy 已完成 dir
    try:
        root_children_all = (
            await pikpak_service.list_files(root_id, size=500) if root_id else []
        )
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": f"列出 {root_path} 失敗: {exc}"}
        return
    skip_names = _avbt_root_skip_names()
    root_children = [c for c in root_children_all if c.name not in skip_names]

    try:
        legacy_children = (
            await pikpak_service.list_files(legacy_id, size=500)
            if legacy_id else []
        )
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": f"列出 {legacy_path} 失敗: {exc}"}
        return

    # Resolve every tracked listing's destination folder. folder_id auto-
    # creates, so an empty tracked listing just yields an empty folder
    # with zero work — harmless. Uses the Chinese kind label to match
    # what the archiver actually writes to (AVBT/系列/X not AVBT/series/X).
    async with SessionLocal() as session:
        tracked_rows = (
            await session.execute(select(TrackedListing))
        ).scalars().all()

    cleanup_targets: list[tuple[str, str, list]] = []
    seen_target_paths: set[str] = set()

    # Pass 1: tracked listings (use their explicit safe-name path so
    # rename quirks vs the actual folder name still resolve via lookup).
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
            seen_target_paths.add(target_path)

    # Pass 2: walk every kind base path and pick up sibling name-folders
    # the tracked-listing pass missed — stale folders, listings the user
    # never tracked, or tracked rows whose name no longer matches the
    # on-disk folder name. Cleanup is identical: dedupe / flatten /
    # rename children inside ``<kind>/<name>/``.
    for _kind, kind_path in all_kind_paths():
        try:
            kind_id = await pikpak_service.lookup_folder_id(kind_path)
        except Exception:  # noqa: BLE001
            continue
        if not kind_id:
            continue
        try:
            name_dirs = await pikpak_service.list_files(kind_id, size=500)
        except Exception as exc:  # noqa: BLE001
            logger.warning("can't list %s: %s", kind_path, exc)
            continue
        for nd in name_dirs:
            if nd.kind != "drive#folder":
                continue
            target_path = f"{kind_path}/{nd.name}"
            if target_path in seen_target_paths:
                continue
            try:
                children = await pikpak_service.list_files(nd.id, size=500)
            except Exception as exc:  # noqa: BLE001
                logger.warning("can't list %s: %s", target_path, exc)
                continue
            if children:
                cleanup_targets.append((target_path, nd.id, children))
                seen_target_paths.add(target_path)

    total = (
        len(root_children)
        + len(legacy_children)
        + sum(len(c) for _, _, c in cleanup_targets)
    )

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

    # ---- Phase 1a: migrate AVBT root ----
    async for ev in _phase1_migrate_root(dry_run=dry_run, idx_start=idx):
        if ev.get("type") == "_phase1_error":
            yield {"type": "error", "message": ev.get("message", "")}
            continue
        if ev.get("type") == "_phase1_total":
            continue
        action = ev.get("action")
        if action == "move":
            summary["moved"] += 1
        elif action == "rename":
            summary["renamed"] += 1
        elif action == "skip":
            summary["skipped"] += 1
        elif action == "error":
            summary["errors"] += 1
        idx = ev.get("current", idx)
        yield ev

    # ---- Phase 1b: migrate legacy 已完成 ----
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
        elif action == "rename":
            summary["renamed"] += 1
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
