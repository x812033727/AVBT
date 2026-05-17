"""Periodically move completed PikPak offline files to a per-code folder.

Runs as a background asyncio task started by FastAPI's lifespan.

For every row in ``offline_task_log`` where:
    * the task has a code (so we know where to put it)
    * the PikPak task is in PHASE_TYPE_COMPLETE
    * the row is not yet ``archived``

we ensure ``<pikpak_archive_folder>/<code>/`` exists and ``file_batch_move``
the resulting file_id there. The row is then flagged archived so we don't
move it twice.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select

from ..config import kind_base_path, settings
from ..database import SessionLocal
from ..models import OfflineTaskLog, TrackedListing
from ..scrapers import javbus as scraper
from .notify import send_webhook
from .pikpak import PikPakError, pikpak_service

logger = logging.getLogger(__name__)

_SAFE_CODE = re.compile(r"[^A-Za-z0-9_\-]+")

# Hierarchy priority — when a code belongs to multiple tracked listings,
# the leftmost match wins. Most-specific first.
_KIND_PRIORITY = ("series", "director", "label", "studio", "star")


def _safe_code(code: str) -> str:
    """Sanitise a code so it can be used as a folder name."""
    return _SAFE_CODE.sub("", code.strip())[:64]


# Delegate to the shared helper so missing-code services can compute the
# same path without importing the archiver (which would cycle).
from .jav_code import safe_folder_name as _safe_name  # noqa: E402


# A small per-pass cache so two completed tasks with the same code don't
# trigger two JavBus fetches.
_detail_cache: dict[str, object] = {}


def _detail_kinds(detail) -> dict[str, tuple[str, str]]:
    """From a MovieDetail, return {kind: (slug, name)} for whatever
    listing-kind attributes are populated."""
    out: dict[str, tuple[str, str]] = {}
    for kind in ("series", "director", "label", "studio"):
        ref = getattr(detail, kind, None)
        if ref and getattr(ref, "id", "") and getattr(ref, "name", ""):
            out[kind] = (ref.id, ref.name)
    for actress in getattr(detail, "actresses", None) or []:
        if actress.id and actress.name:
            out["star"] = (actress.id, actress.name)
            break  # use the first credited actress
    return out


async def _resolve_archive_path(code: str) -> str:
    """Pick the destination folder for ``code`` based on TrackedListing
    membership. Falls back to ``pikpak_archive_folder/<code>`` when no
    tracked listing matches (or detail lookup fails)."""
    safe_code = _safe_code(code)
    fallback = f"{settings.pikpak_archive_folder}/{safe_code}"

    detail = _detail_cache.get(code)
    if detail is None:
        try:
            detail = await scraper.fetch_detail(code)
        except Exception as exc:  # noqa: BLE001
            logger.debug("fetch_detail(%s) failed: %s", code, exc)
            return fallback
        _detail_cache[code] = detail

    kinds = _detail_kinds(detail)  # type: ignore[arg-type]
    if not kinds:
        return fallback

    async with SessionLocal() as session:
        for kind in _KIND_PRIORITY:
            ref = kinds.get(kind)
            if not ref:
                continue
            slug, name = ref
            row = await session.get(TrackedListing, (kind, slug))
            if row is None:
                continue
            safe = _safe_name(name, fallback=_safe_name(slug, fallback="unknown"))
            # kind_base_path() returns AVBT/<chinese kind label> by default
            # (matching the archiver's natural-language layout) and honours
            # per-kind env overrides like PIKPAK_SERIES_FOLDER.
            return f"{kind_base_path(kind)}/{safe}/{safe_code}"

    return fallback


class ArchiverState:
    def __init__(self) -> None:
        self.enabled: bool = settings.archive_enabled
        self.last_run: datetime | None = None
        self.archived_total: int = 0
        self.last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "interval_seconds": settings.archive_interval_seconds,
            "archive_folder": settings.pikpak_archive_folder,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "archived_total": self.archived_total,
            "last_error": self.last_error,
            "sweep_enabled": settings.archive_sweep_root_enabled,
            "sweep_interval_seconds": settings.archive_sweep_interval_seconds,
            "last_sweep_at": (
                _last_sweep_at.isoformat() if _last_sweep_at else None
            ),
            "last_sweep_moved": _last_sweep_moved,
            "last_sweep_error": _last_sweep_error,
            "sweep_swept_total": _swept_total,
        }


state = ArchiverState()


# Module-level cooldown state for the AVBT-root sweep. Kept separate
# from ArchiverState so we can mutate from free functions without
# threading the instance through.
_last_sweep_at: datetime | None = None
_last_sweep_moved: int = 0
_last_sweep_error: str = ""
_swept_total: int = 0


def _sweep_due() -> bool:
    """True when the AVBT-root sweep should run now (interval elapsed
    or never run yet). Respects the on/off setting."""
    if not settings.archive_sweep_root_enabled:
        return False
    if _last_sweep_at is None:
        return True
    interval = max(60, settings.archive_sweep_interval_seconds)
    return (datetime.utcnow() - _last_sweep_at).total_seconds() >= interval


async def _sweep_root_once() -> int:
    """Reuse the reorganize phase-1 logic to tidy orphans that landed
    in the AVBT root outside the OfflineTaskLog flow (PikPak App / web
    manual adds, magnets dropped straight into PikPak). Then, for each
    wrapper we just moved, rerun the phase-2 flatten so the user sees
    the canonical ``<code>.<ext>`` in the target folder instead of a
    BT-noise wrapper. Finally mark matching OfflineTaskLog rows as
    archived so the DB-driven pass doesn't try to re-move them.

    Returns the number of root children moved.

    Streaming events from ``_phase1_migrate_root`` are consumed silently
    — this is a background tidy, not a UI flow — but ``_phase1_error``
    events are surfaced into ``_last_sweep_error`` so settings/UI can
    show what went wrong (the migrate generator signals init failures
    via that event type instead of raising)."""
    global _last_sweep_at, _last_sweep_moved, _last_sweep_error, _swept_total

    # Local import: reorganize already imports archiver at module load,
    # so a top-level import here would cycle.
    from .reorganize import _phase1_migrate_root

    moved = 0
    sweep_error = ""
    # All source_ids we moved — used to mark OfflineTaskLog archived.
    moved_ids: list[str] = []
    # (folder_id, parent_id, code, leaf) tuples — used to phase-2
    # flatten each just-moved wrapper at its new location.
    moved_wrappers: list[tuple[str, str, str, str]] = []

    try:
        async for ev in _phase1_migrate_root(dry_run=False, idx_start=0):
            ev_type = ev.get("type")
            if ev_type == "_phase1_error":
                # First _phase1_error wins so the UI sees the original
                # failure rather than a downstream cascade message.
                sweep_error = sweep_error or ev.get("message", "")
                continue
            if ev_type != "progress":
                continue
            if ev.get("action") != "move":
                continue
            moved += 1
            sid = ev.get("source_id")
            if not sid:
                continue
            moved_ids.append(sid)
            if ev.get("kind") == "folder":
                pid = ev.get("target_parent_id")
                code = ev.get("code")
                leaf = ev.get("leaf")
                if pid and code and leaf:
                    moved_wrappers.append((sid, pid, code, leaf))
    except Exception as exc:  # noqa: BLE001
        sweep_error = str(exc)
        logger.warning("root sweep failed: %s", exc)
    finally:
        _last_sweep_at = datetime.utcnow()
        _last_sweep_moved = moved
        _last_sweep_error = sweep_error
        _swept_total += moved

    # Phase-2 flatten on every wrapper we just moved. Extracts the main
    # video to the kind/name folder, trashes inner clutter + the wrapper.
    # PikPak's trash is recoverable for ~30 days, so a misjudgement is
    # not destructive.
    if moved_wrappers:
        try:
            flattened = await _flatten_swept_wrappers(moved_wrappers)
            if flattened:
                logger.info(
                    "root sweep flattened %d wrapper(s)", flattened
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("flatten swept wrappers failed: %s", exc)

    # Stop the DB-driven pass from re-moving what we just moved. Without
    # this, every loop iteration retries the move (PikPak rejects it
    # because the file's no longer a child of AVBT root) and the log
    # fills with "move failed" warnings.
    if moved_ids:
        try:
            await _mark_offline_log_archived(moved_ids)
        except Exception as exc:  # noqa: BLE001
            logger.warning("offline log sync failed: %s", exc)

    if moved:
        logger.info("root sweep moved %d orphan(s) to kind/name", moved)
        try:
            from .pikpak_presence import presence_index  # avoid cycle
            presence_index.invalidate()
        except Exception:  # noqa: BLE001
            pass
        # Folder cache may now point at trashed/renamed wrappers; drop
        # so the next folder_id() relists. reorganize_stream does the
        # same after its mutating runs.
        try:
            pikpak_service._folder_cache.clear()
        except Exception:  # noqa: BLE001
            pass
    return moved


async def _flatten_swept_wrappers(
    wrappers: list[tuple[str, str, str, str]],
) -> int:
    """Rerun phase-2 flatten on each just-moved wrapper. Synthesises a
    PikPakFile stub from the post-move metadata — _resolve_folder_winner
    only reads .id / .name / .kind, so a fresh server fetch is
    unnecessary. Returns the count of wrappers actually flattened."""
    from .reorganize import _resolve_folder_winner
    from ..schemas import PikPakFile

    flattened = 0
    for folder_id, parent_id, code, leaf in wrappers:
        folder = PikPakFile(
            id=folder_id,
            name=leaf,
            kind="drive#folder",
            size=None,
        )
        try:
            result = await _resolve_folder_winner(
                folder, code, parent_id, dry_run=False
            )
            if result.get("action") == "flatten":
                flattened += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "flatten swept wrapper %s (%s) failed: %s",
                folder_id, code, exc,
            )
    return flattened


async def _mark_offline_log_archived(file_ids: list[str]) -> None:
    """Mark OfflineTaskLog rows for ``file_ids`` archived so the
    DB-driven pass skips them (the sweep already moved the files)."""
    if not file_ids:
        return
    from sqlalchemy import update

    async with SessionLocal() as session:
        await session.execute(
            update(OfflineTaskLog)
            .where(
                OfflineTaskLog.file_id.in_(file_ids),
                OfflineTaskLog.archived.is_(False),
            )
            .values(archived=True, archived_at=datetime.utcnow())
        )
        await session.commit()


async def archive_once() -> int:
    """Run one archive pass. Returns the number of files moved."""
    state.last_error = ""
    if not state.enabled or not settings.pikpak_username:
        return 0

    # Catch orphans that never went through OfflineTaskLog (manual
    # PikPak App / web adds, leftover files). Runs on its own cooldown
    # so we don't bombard PikPak / JavBus every 15 s when the AVBT root
    # has nothing new to sweep. Independent of list_tasks: a sweep
    # should still happen even if the task list itself is unreachable
    # or has no completed entries.
    if _sweep_due():
        try:
            await _sweep_root_once()
        except Exception as exc:  # noqa: BLE001
            state.last_error = f"sweep_root failed: {exc}"
            logger.warning("sweep_root failed: %s", exc)

    try:
        tasks = await pikpak_service.list_tasks(size=200)
    except PikPakError as exc:
        state.last_error = f"list_tasks failed: {exc}"
        return 0
    except Exception as exc:  # noqa: BLE001
        state.last_error = str(exc)
        return 0

    completed = {
        t.file_id: t
        for t in tasks
        if t.file_id and t.phase == "PHASE_TYPE_COMPLETE"
    }
    if not completed:
        return 0

    moved = 0
    # Reset per-pass detail cache.
    _detail_cache.clear()
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(OfflineTaskLog).where(
                    OfflineTaskLog.file_id.in_(list(completed.keys())),
                    OfflineTaskLog.archived.is_(False),
                    OfflineTaskLog.code != "",
                )
            )
        ).scalars().all()

        notifications: list[str] = []
        for row in rows:
            if not _safe_code(row.code):
                continue
            try:
                target_path = await _resolve_archive_path(row.code)
                target_id = await pikpak_service.folder_id(target_path)
                if not target_id:
                    continue
                await pikpak_service.move_files([row.file_id], target_id)
                row.archived = True
                row.archived_at = datetime.utcnow()
                moved += 1
                notifications.append(
                    f"📦 已歸檔 `{row.code}` ({row.name or row.file_id}) → `{target_path}`"
                )
                logger.info("archived %s -> %s", row.file_id, target_path)
            except Exception as exc:  # noqa: BLE001
                state.last_error = f"move {row.file_id} failed: {exc}"
                logger.warning("archive %s failed: %s", row.file_id, exc)

        if moved:
            await session.commit()
            # Newly-archived codes change which codes count as "present".
            try:
                from .pikpak_presence import presence_index  # avoid cycle
                presence_index.invalidate()
            except Exception:  # noqa: BLE001
                pass
            for msg in notifications:
                asyncio.create_task(send_webhook(msg))

    state.archived_total += moved
    return moved


async def run_loop() -> None:
    """Background loop. Sleeps between iterations; survives errors."""
    consecutive_errors = 0
    while True:
        try:
            await archive_once()
            consecutive_errors = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            consecutive_errors += 1
            state.last_error = str(exc)
            logger.exception("archiver loop iteration failed")
        finally:
            state.last_run = datetime.utcnow()
        base = max(15, settings.archive_interval_seconds)
        backoff = min(4, 2 ** consecutive_errors) if consecutive_errors else 1
        jitter = random.uniform(0, min(10, base / 10))
        await asyncio.sleep(base * backoff + jitter)
