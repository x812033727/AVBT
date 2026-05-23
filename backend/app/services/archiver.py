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
from typing import Any, AsyncIterator

from sqlalchemy import func, select

from ..config import kind_base_path, settings, task_folder_path
from ..database import SessionLocal
from ..models import OfflineTaskLog, TrackedListing
from ..scrapers import javbus as scraper
from .pikpak import PikPakError, pikpak_service
from .webhook_queue import webhook_queue

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


async def resolve_listing_loose(
    code: str,
    *,
    priority: tuple[str, ...] = ("series", "label", "studio"),
) -> tuple[str, str] | None:
    """Decide which JavBus listing a code belongs to, **without** the
    TrackedListing membership requirement that :func:`resolve_listing_for_code`
    imposes.

    Walks ``priority`` in order and returns the first listing kind that
    JavBus has detail for. Used by pCloud organize: the goal there is
    "categorise everything we have under a sensible folder" — there's
    no PikPak-style download flow to gate on whether the user is
    actively tracking a series, so we just take the strongest hint
    JavBus gives us. Default priority matches the user-requested
    fallback chain: series → label (發行商) → studio (製作商).

    Shares ``_detail_cache`` with the strict resolver, so a single
    pCloud pass and a concurrent PikPak archiver pass don't re-fetch
    the same code twice.

    Returns ``(kind, safe_name)`` or ``None`` when JavBus has no
    detail at all (fetch failure / 404) **or** has detail but none of
    the requested kinds are populated.
    """
    detail = _detail_cache.get(code)
    if detail is None:
        try:
            detail = await scraper.fetch_detail(code)
        except Exception as exc:  # noqa: BLE001
            logger.debug("fetch_detail(%s) failed: %s", code, exc)
            return None
        _detail_cache[code] = detail

    kinds = _detail_kinds(detail)  # type: ignore[arg-type]
    for kind in priority:
        ref = kinds.get(kind)
        if not ref:
            continue
        slug, name = ref
        safe = _safe_name(
            name, fallback=_safe_name(slug, fallback="unknown")
        )
        return kind, safe
    return None


async def resolve_listing_for_code(code: str) -> tuple[str, str] | None:
    """Decide which tracked listing a JAV code belongs to.

    Looks up the code in JavBus (with ``_detail_cache`` memoisation),
    enumerates the candidate ``(kind, slug, name)`` triples from the
    movie detail, then walks ``_KIND_PRIORITY`` and returns the FIRST
    triple whose ``(kind, slug)`` exists in the ``TrackedListing`` table.

    Returns ``(kind, safe_name)`` where ``safe_name`` has already been
    run through the same ``_safe_name`` fallback chain that the archiver
    uses for path components — so callers can plug it straight into a
    filesystem / pCloud path without re-sanitising.

    Returns ``None`` if JavBus fetch fails, the detail has no listing
    refs, or no listing kind matches a tracked row. Callers decide what
    that means (archiver falls back to ``pikpak_archive_folder``; pCloud
    organize skips with ``reason=no_tracked_match``)."""
    detail = _detail_cache.get(code)
    if detail is None:
        try:
            detail = await scraper.fetch_detail(code)
        except Exception as exc:  # noqa: BLE001
            logger.debug("fetch_detail(%s) failed: %s", code, exc)
            return None
        _detail_cache[code] = detail

    kinds = _detail_kinds(detail)  # type: ignore[arg-type]
    if not kinds:
        return None

    async with SessionLocal() as session:
        for kind in _KIND_PRIORITY:
            ref = kinds.get(kind)
            if not ref:
                continue
            slug, name = ref
            tracked_row = await session.get(TrackedListing, (kind, slug))
            if tracked_row is None:
                continue
            # Prefer the tracked-listing row's stored name over whatever
            # JavBus returned on this fetch — JavBus markup / template /
            # language can shift slightly (spacing, half/full-width
            # punctuation) and yields differently-spelled folder names
            # for the same listing on consecutive runs. The row.name was
            # user-confirmed at tracking time and is the stable choice.
            safe = _safe_name(
                tracked_row.name or name,
                fallback=_safe_name(name, fallback=_safe_name(slug, fallback="unknown")),
            )
            return kind, safe

    return None


async def _resolve_archive_path_by_code(code: str) -> str:
    """JavBus-driven path resolution. Returns the fallback path when
    JavBus fails or no kind matches a tracked listing.

    Used by reorganize (no OfflineTaskLog row context at all) and by
    ``_resolve_archive_path`` when the row's tracked_* snapshot is empty
    (manual submits, rows that predate the snapshot columns)."""
    safe_code = _safe_code(code)
    resolved = await resolve_listing_for_code(code)
    if resolved is None:
        return f"{settings.pikpak_archive_folder}/{safe_code}"
    kind, safe_name = resolved
    # kind_base_path() returns AVBT/<chinese kind label> by default
    # (matching the archiver's natural-language layout) and honours
    # per-kind env overrides like PIKPAK_SERIES_FOLDER.
    return f"{kind_base_path(kind)}/{safe_name}/{safe_code}"


async def _resolve_archive_path(row: OfflineTaskLog) -> str:
    """Pick the destination folder for ``row.code``. Fast path: when
    enqueue captured the tracked listing context, build the path
    directly without an external HTTP call. Slow path: delegate to
    ``_resolve_archive_path_by_code`` which hits JavBus."""
    code = row.code
    safe_code = _safe_code(code)

    snap_kind = (row.tracked_kind or "").strip()
    snap_slug = (row.tracked_slug or "").strip()
    snap_name = (row.tracked_name or "").strip()
    if snap_kind and snap_slug and snap_name:
        safe = _safe_name(
            snap_name,
            fallback=_safe_name(snap_slug, fallback="unknown"),
        )
        return f"{kind_base_path(snap_kind)}/{safe}/{safe_code}"

    return await _resolve_archive_path_by_code(code)


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
            "task_folder": task_folder_path(),
            "sweep_fallback_root": settings.pikpak_sweep_fallback_root,
            "legacy_sweep_enabled": settings.archive_sweep_legacy_enabled,
            "last_legacy_sweep_at": (
                _last_legacy_sweep_at.isoformat() if _last_legacy_sweep_at else None
            ),
            "last_legacy_sweep_moved": _last_legacy_sweep_moved,
            "last_legacy_sweep_error": _last_legacy_sweep_error,
            "legacy_sweep_swept_total": _legacy_swept_total,
        }


state = ArchiverState()


# Module-level cooldown state for the AVBT-root sweep. Kept separate
# from ArchiverState so we can mutate from free functions without
# threading the instance through.
_last_sweep_at: datetime | None = None
_last_sweep_moved: int = 0
_last_sweep_error: str = ""
_swept_total: int = 0

# Same idea, but for the legacy-archive (``AVBT/已完成``) sweep that
# re-evaluates parked codes against the current TrackedListing set.
_last_legacy_sweep_at: datetime | None = None
_last_legacy_sweep_moved: int = 0
_last_legacy_sweep_error: str = ""
_legacy_swept_total: int = 0


def _sweep_due() -> bool:
    """True when the AVBT-root sweep should run now (interval elapsed
    or never run yet). Respects the on/off setting."""
    if not settings.archive_sweep_root_enabled:
        return False
    if _last_sweep_at is None:
        return True
    interval = max(60, settings.archive_sweep_interval_seconds)
    return (datetime.utcnow() - _last_sweep_at).total_seconds() >= interval


def _legacy_sweep_due() -> bool:
    """True when the legacy-archive sweep should run now. Shares the
    root-sweep cadence so the user can think of both as "background
    tidy" with one knob, but is independently disable-able."""
    if not settings.archive_sweep_legacy_enabled:
        return False
    if _last_legacy_sweep_at is None:
        return True
    interval = max(60, settings.archive_sweep_interval_seconds)
    return (datetime.utcnow() - _last_legacy_sweep_at).total_seconds() >= interval


async def _sweep_legacy_archive_stream() -> AsyncIterator[dict]:
    """Streaming variant of ``_sweep_legacy_archive_once``: yields
    ``start`` / ``progress`` / ``error`` / ``done`` events so the UI can
    show which file is being processed and where it's stuck.

    Walks ``AVBT/已完成`` and migrates codes that *now* match a tracked
    listing. Reuses ``reorganize._phase1_migrate_from`` — same logic the
    manual "整理 PikPak 資料夾" button uses for its Phase 1b. After
    Phase 1, runs ``_cleanup_target_parents`` on the destination folders
    so multipart files (``ABC-001-1.mp4`` / ``ABC-001CD1.mp4`` etc.)
    get renamed to ``ABC-001_1.mp4`` instead of colliding into
    ``ABC-001 (2).mp4``.

    Deliberately does **not** call ``_flatten_swept_wrappers``: legacy
    items often live inside wrapper folders the user has kept on
    purpose, and the flattener picks one "winner" video and trashes
    the rest — which would destroy real multipart episodes that
    happen to share a wrapper.

    Updates the module-level ``_last_legacy_sweep_*`` state at the end
    so ``/archiver`` status reflects this run."""
    global _last_legacy_sweep_at, _last_legacy_sweep_moved
    global _last_legacy_sweep_error, _legacy_swept_total

    # Local import: reorganize already imports archiver at module load,
    # so a top-level import here would cycle.
    from .reorganize import _phase1_migrate_from, _phase2_cleanup_target

    legacy_path = (
        settings.pikpak_archive_folder or "AVBT/已完成"
    ).strip().strip("/")
    if not legacy_path:
        yield {
            "type": "done",
            "result": {
                "total": 0, "moved": 0, "skipped": 0, "errors": 0,
                "source": "",
            },
        }
        return

    # Pre-flight: count children so the progress bar has a denominator.
    total = 0
    try:
        source_id = await pikpak_service.folder_id(legacy_path)
        if source_id:
            children = await pikpak_service.list_files(source_id, size=500)
            total = len(children)
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": f"無法列出 {legacy_path}: {exc}"}
        yield {
            "type": "done",
            "result": {
                "total": 0, "moved": 0, "skipped": 0, "errors": 1,
                "source": legacy_path,
            },
        }
        return

    yield {
        "type": "start",
        "total": total,
        "source": legacy_path,
    }

    moved = 0
    skipped = 0
    errors = 0
    sweep_error = ""
    idx = 0
    target_parent_ids: set[str] = set()

    try:
        async for ev in _phase1_migrate_from(
            legacy_path, dry_run=False, idx_start=0
        ):
            ev_type = ev.get("type")
            if ev_type == "_phase1_error":
                msg = ev.get("message", "")
                sweep_error = sweep_error or msg
                errors += 1
                yield {"type": "error", "message": msg}
                continue
            if ev_type == "_phase1_total":
                continue
            if ev_type != "progress":
                continue
            action = ev.get("action")
            if action == "move":
                moved += 1
                pid = ev.get("target_parent_id")
                if pid:
                    target_parent_ids.add(pid)
            elif action == "skip":
                skipped += 1
            elif action == "error":
                errors += 1
                logger.warning(
                    "legacy sweep file %s failed: %s",
                    ev.get("source"), ev.get("reason"),
                )
            idx = ev.get("current", idx)
            yield ev
    except Exception as exc:  # noqa: BLE001
        sweep_error = str(exc)
        errors += 1
        logger.warning("legacy sweep failed: %s", exc)
        yield {"type": "error", "message": str(exc)}
    finally:
        _last_legacy_sweep_at = datetime.utcnow()
        _last_legacy_sweep_moved = moved
        _last_legacy_sweep_error = sweep_error
        _legacy_swept_total += moved

    # Phase-2 cleanup: catches `ABC-001-1.mp4` / `ABC-001CD1.mp4` style
    # multipart so they get unified into `ABC-001_1.mp4` form. Skips
    # _flatten_swept_wrappers on purpose (see docstring).
    cleanup_count = 0
    for pid in target_parent_ids:
        try:
            children = await pikpak_service.list_files(pid, size=500)
            if not children:
                continue
            async for ev in _phase2_cleanup_target(
                pid, pid, children, dry_run=False, idx_start=idx
            ):
                ev_type = ev.get("type")
                if ev_type == "progress":
                    idx = ev.get("current", idx)
                    if ev.get("action") == "error":
                        errors += 1
                yield ev
            cleanup_count += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("cleanup target %s failed: %s", pid, exc)
            yield {
                "type": "error",
                "message": f"cleanup {pid}: {exc}",
            }
    if cleanup_count:
        logger.info(
            "legacy sweep cleaned %d target folder(s)", cleanup_count
        )

    if moved:
        logger.info(
            "legacy sweep promoted %d code(s) from %s to kind/name",
            moved, legacy_path,
        )
        try:
            from . import missing as missing_svc  # avoid cycle
            await missing_svc.invalidate_all_caches_async(presence=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            pikpak_service._folder_cache.clear()
        except Exception:  # noqa: BLE001
            pass

    yield {
        "type": "done",
        "result": {
            "total": total,
            "moved": moved,
            "skipped": skipped,
            "errors": errors,
            "source": legacy_path,
        },
    }


async def _sweep_legacy_archive_once() -> int:
    """Background-loop friendly wrapper around
    ``_sweep_legacy_archive_stream``: silently consumes events and
    returns the count moved.

    Items without a tracked match stay parked (``_phase1_migrate_from``
    skips them with ``reason=no_tracked_match``). Safe to call repeatedly:
    once a code is moved out, subsequent sweeps won't see it again."""
    moved = 0
    async for ev in _sweep_legacy_archive_stream():
        if (
            ev.get("type") == "progress"
            and ev.get("action") == "move"
        ):
            moved += 1
    return moved


async def _sweep_root_once(*, cleanup_all_targets: bool = False) -> int:
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
    via that event type instead of raising).

    ``cleanup_all_targets``: when True, also run phase-2 cleanup on
    every tracked series folder (not just the ones this sweep moved
    items into). Used by the user-triggered "掃描 TASK 並搬移" button
    so one click normalises everything; background loop leaves it False
    to keep per-cycle cost low."""
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
    # Distinct target folders that received anything this sweep — used
    # to rerun phase-2 cleanup over their contents (multipart rename,
    # BT-prefix strip, wrapper flatten retry for items that finished
    # downloading after their first sweep move).
    target_parent_ids: set[str] = set()

    async def _consume(source_path: str | None) -> None:
        nonlocal moved, sweep_error
        async for ev in _phase1_migrate_root(
            dry_run=False, idx_start=0, source_path=source_path
        ):
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
            pid = ev.get("target_parent_id")
            if pid:
                target_parent_ids.add(pid)
            if ev.get("kind") == "folder":
                code = ev.get("code")
                leaf = ev.get("leaf")
                if pid and code and leaf:
                    moved_wrappers.append((sid, pid, code, leaf))

    try:
        # Primary: dedicated task folder (where new offline tasks land).
        await _consume(None)
        # Fallback: AVBT root, for magnets the user submitted via the
        # PikPak App/web (which bypass the backend and ignore the task
        # folder setting). Off by default so legacy installs don't get
        # noisy double-scans.
        if settings.pikpak_sweep_fallback_root:
            task_path = task_folder_path()
            root_path = (
                settings.pikpak_download_folder or "AVBT"
            ).strip().strip("/")
            if root_path and root_path != task_path:
                await _consume(root_path)
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

    # When the user explicitly triggered the sweep, also pull in every
    # tracked series target folder so messy leftovers from previous
    # sweeps (BT-prefixed names, CD<n>/-<n> markers that pre-date the
    # canonical fix, wrappers that finished downloading after their
    # original flatten attempt failed) all get normalised in one click.
    if cleanup_all_targets:
        try:
            target_parent_ids |= await _all_tracked_target_parent_ids()
        except Exception as exc:  # noqa: BLE001
            logger.warning("collect tracked targets failed: %s", exc)

    # Rerun phase-2 cleanup on every target folder that received items.
    # Catches:
    #   - Wrappers whose main video finished downloading after the
    #     initial flatten attempt couldn't find one
    #   - Same-code variants spread across BT prefixes / CD<n> / -<n>
    #     suffixes — they get unified into ``<code>_<N>.<ext>``
    #   - Singletons with BT-prefix noise get renamed to bare ``<code>``
    if target_parent_ids:
        try:
            cleaned = await _cleanup_target_parents(target_parent_ids)
            if cleaned:
                logger.info(
                    "root sweep cleaned %d target folder(s)", cleaned
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("cleanup target parents failed: %s", exc)

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
            from . import missing as missing_svc  # avoid cycle
            await missing_svc.invalidate_all_caches_async(presence=True)
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


async def _all_tracked_target_parent_ids() -> set[str]:
    """Resolve every tracked listing's series-name folder id (``AVBT/
    <kind>/<row.name>/``). Folders that don't exist yet are skipped
    (lookup-only, no auto-create) so we don't pollute PikPak with
    empty placeholders for listings the user added but never had
    any download for."""
    from ..config import kind_base_path

    ids: set[str] = set()
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(TrackedListing))
        ).scalars().all()

    for row in rows:
        if not (row.name or "").strip():
            continue
        safe = _safe_name(
            row.name, fallback=_safe_name(row.id, fallback="unknown")
        )
        target_path = f"{kind_base_path(row.kind)}/{safe}"
        try:
            pid = await pikpak_service.lookup_folder_id(target_path)
        except Exception:  # noqa: BLE001
            continue
        if pid:
            ids.add(pid)
    return ids


async def _cleanup_target_parents(parent_ids: set[str]) -> int:
    """Run phase-2 cleanup on each target folder we moved items into:
    multipart rename, BT-prefix strip, wrapper flatten retry, dedupe.

    Same logic the user gets when clicking "整理 PikPak 資料夾", but
    scoped to just the folders this sweep touched — so we don't grind
    over every series folder every 5 minutes.

    Returns the count of folders we successfully traversed."""
    from .reorganize import _phase2_cleanup_target

    cleaned = 0
    for pid in parent_ids:
        try:
            children = await pikpak_service.list_files(pid, size=500)
            if not children:
                continue
            async for _ev in _phase2_cleanup_target(
                pid, pid, children, dry_run=False, idx_start=0
            ):
                pass  # silent consume — this is background tidying
            cleaned += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("cleanup target %s failed: %s", pid, exc)
    return cleaned


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

    # Re-evaluate parked codes in AVBT/已完成: if the user just started
    # tracking the relevant series/star, promote them out of the fallback
    # bucket into the proper kind/name folder. No-op when nothing matches.
    if _legacy_sweep_due():
        try:
            await _sweep_legacy_archive_once()
        except Exception as exc:  # noqa: BLE001
            state.last_error = f"sweep_legacy failed: {exc}"
            logger.warning("sweep_legacy failed: %s", exc)

    # Cheap DB peek: if nothing has been submitted-but-not-archived
    # since the last pass, skip the PikPak list_tasks round-trip
    # entirely. The sweep above is independent — orphans in the TASK
    # folder still get tidied on their own cooldown even when no row
    # is pending.
    async with SessionLocal() as session:
        pending = (
            await session.execute(
                select(func.count(OfflineTaskLog.id)).where(
                    OfflineTaskLog.archived.is_(False),
                    OfflineTaskLog.file_id != "",
                    OfflineTaskLog.code != "",
                )
            )
        ).scalar() or 0
    if pending == 0:
        return 0

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
                target_path = await _resolve_archive_path(row)
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
                from . import missing as missing_svc  # avoid cycle
                await missing_svc.invalidate_all_caches_async(presence=True)
            except Exception:  # noqa: BLE001
                pass
            for msg in notifications:
                webhook_queue.enqueue_nowait(msg)

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
