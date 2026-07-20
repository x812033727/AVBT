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
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select

from ..config import (
    kind_base_path,
    settings,
    task_folder_path,
    untracked_studio_base_path,
)
from ..database import SessionLocal
from ..models import MovieDetailCache, OfflineTaskLog, TrackedListing
from ..schemas import MovieDetail
from ..scrapers import javbus as scraper
from .pikpak import ACTIVE_PHASES, PikPakError, pikpak_service
from .webhook_queue import webhook_queue

logger = logging.getLogger(__name__)

_SAFE_CODE = re.compile(r"[^A-Za-z0-9_\-]+")

# Hierarchy priority — when a code belongs to multiple tracked listings,
# the leftmost match wins. Most-specific first. Only used for the
# no-studio fallback now that studio→series nesting is the primary layout.
_KIND_PRIORITY = ("series", "director", "label", "studio", "star")

# Folder name for a studio's movies that have no series (keeps the
# 製作商/<studio>/<series>/<code> depth uniform). Mirrors
# ``studio_index.NO_SERIES_NAME`` so the browse page and the physical
# layout agree.
_NO_SERIES_FOLDER = "未分類"


def _safe_code(code: str) -> str:
    """Sanitise a code so it can be used as a folder name."""
    return _SAFE_CODE.sub("", code.strip())[:64]


# Delegate to the shared helpers so missing-code services can compute the
# same path without importing the archiver (which would cycle).
from .jav_code import (  # noqa: E402
    extract_jav_code_full,
)
from .jav_code import (  # noqa: E402
    safe_folder_name as _safe_name,
)


def _archive_leaf(code: str) -> str:
    """Canonical folder/file leaf for an archived code.

    Strips the numeric BT/maker prefix (``259LUXU-1543`` → ``LUXU-1543``)
    so amateur-label folders match the rest of the system's stripped
    convention — the presence index, listing codes and the reorganize
    sweep all key off the prefix-stripped form. Keeps any trailing A/B/C
    variant letter (``extract_jav_code_full``) so distinct variants don't
    collide on the same folder. Falls back to char-sanitising the raw code
    when extraction finds nothing parseable."""
    return _safe_code(extract_jav_code_full(code) or code)


# Detail lookups delegate caching to the scraper: its in-memory cache
# (30-min TTL + in-flight coalescing) dedups within a pass, and the
# persistent movie_detail_cache table dedups across passes and restarts.

# file_ids whose archive failure was already notified — the archive loop
# retries every minute, so without this a permanently-stuck file would
# push a notification per pass. Process-lifetime is fine (a restart
# re-notifying once is acceptable).
_failure_notified: set[str] = set()


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

    Caching is the scraper's job (in-memory + persistent table), so a
    single pCloud pass and a concurrent PikPak archiver pass don't
    re-fetch the same code twice.

    Returns ``(kind, safe_name)`` or ``None`` when JavBus has no
    detail at all (fetch failure / 404) **or** has detail but none of
    the requested kinds are populated.
    """
    try:
        detail = await scraper.fetch_detail_resolved(code)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch_detail(%s) failed: %s", code, exc)
        return None

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

    Looks up the code in JavBus (cached by the scraper itself),
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
    try:
        detail = await scraper.fetch_detail_resolved(code)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch_detail(%s) failed: %s", code, exc)
        return None

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


async def _detail_for_archive(
    code: str, *, allow_fetch: bool = True
) -> MovieDetail | None:
    """Get a code's MovieDetail for path routing.

    Reads the persistent ``movie_detail_cache`` row directly, bypassing
    its recency TTL — a movie's studio/series never changes, so a stale
    row is still correct and we avoid an HTTP round-trip at archive time
    (the row almost always exists: it was written during new-work
    detection / backfill). Only when there is no cached row do we fall
    back to a live fetch (which write-throughs the cache)."""
    async with SessionLocal() as session:
        row = await session.get(MovieDetailCache, code)
    if row is not None:
        try:
            return MovieDetail.model_validate_json(row.detail)
        except Exception:  # noqa: BLE001 — corrupt row → try a live fetch
            logger.warning("archive: corrupt detail row for %s", code)
    if not allow_fetch:
        # Callers on a hot path (presence refresh runs per landed code)
        # must not turn a cache miss into a JavBus round-trip.
        return None
    try:
        return await scraper.fetch_detail_resolved(code)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch_detail(%s) failed: %s", code, exc)
        return None


# JavBus renders the SAME studio/series under different spellings on
# different pages (detail pages for old S1 codes say
# 「エスワンナンバーワンスタイル」, newer ones 「エスワン ナンバーワン
# スタイル」 — same listing id 7q). Folder names built straight from the
# detail cache therefore fork one studio into several PikPak folders
# (live mess: three S1 studio folders, 2026-07-15). The tracked listing
# name is refreshed from the listing page and is the single spelling the
# user actually sees, so it wins whenever the ids match.
_tracked_name_cache: dict[tuple[str, str], tuple[str, bool, float]] = {}
_TRACKED_NAME_TTL = 600.0

# JavBus also runs OUTRIGHT DUPLICATE maker pages for the same studio —
# different listing ids, so the tracked-name-by-id defence above can't
# see them (e.g. ROCKET id=ce vs its katakana twin ロケット id=3p9, each
# movie assigned to one page arbitrarily; live fork merged 2026-07-17).
# Alias the duplicate page id to the canonical one BEFORE the tracked
# lookup so both pages resolve to the same folder. The name map covers
# the untracked fallback for the same twins.
_STUDIO_ID_ALIASES: dict[str, str] = {
    "3p9": "ce",  # ロケット → ROCKET
}
_STUDIO_NAME_ALIASES: dict[str, str] = {
    "ロケット": "ROCKET",
}


async def _tracked_listing_info(kind: str, listing_id: str) -> tuple[str, bool]:
    """(current TrackedListing name, row exists) for ``(kind,
    listing_id)``. Cached briefly — archive passes resolve the same
    studio hundreds of times in a row.

    Existence is carried separately from the name: a tracked row CAN
    have an empty name (``POST /api/backup/restore`` writes
    ``name=item.get("name") or ""``), and keying tracked-ness off name
    truthiness would silently route a tracked studio's downloads into
    the untracked tree while /api/studios (row-existence check) shows
    it as tracked."""
    if not listing_id:
        return "", False
    key = (kind, listing_id)
    now = asyncio.get_event_loop().time()
    hit = _tracked_name_cache.get(key)
    if hit is not None and now - hit[2] < _TRACKED_NAME_TTL:
        return hit[0], hit[1]
    name = ""
    exists = False
    try:
        async with SessionLocal() as session:
            row = await session.get(TrackedListing, key)
            if row is not None:
                exists = True
                name = row.name or ""
    except Exception:  # noqa: BLE001 — resolver must never fail the archive
        name, exists = "", False
    _tracked_name_cache[key] = (name, exists, now)
    return name, exists


async def _tracked_listing_name(kind: str, listing_id: str) -> str:
    """Current TrackedListing name for ``(kind, listing_id)``, '' when the
    listing isn't tracked (or tracked with no stored name)."""
    name, _exists = await _tracked_listing_info(kind, listing_id)
    return name


async def _studio_series_dir(detail: MovieDetail) -> str | None:
    """Build the ``<studio_root>/<studio>/<series｜未分類>`` folder (no
    code leaf) when the detail has a studio; ``None`` otherwise.

    kind_base_path("studio") → AVBT/製作商 by default (honours
    PIKPAK_STUDIO_FOLDER override) — the root of the nested layout. The
    same relative shape is reused for the pCloud mirror. Studio / series
    display names prefer the tracked listing's spelling (matched by id)
    so spelling drift between JavBus pages can't fork folders."""
    studio = getattr(detail, "studio", None)
    if not (studio and (getattr(studio, "name", "") or getattr(studio, "id", ""))):
        return None
    studio_id = getattr(studio, "id", "") or ""
    studio_id = _STUDIO_ID_ALIASES.get(studio_id, studio_id)
    raw_name = studio.name or ""
    tracked_name, is_tracked = await _tracked_listing_info("studio", studio_id)
    studio_name = tracked_name or _STUDIO_NAME_ALIASES.get(raw_name, raw_name)
    studio_safe = _safe_name(
        studio_name, fallback=_safe_name(studio_id, fallback="unknown")
    )
    series = getattr(detail, "series", None)
    if series and (getattr(series, "name", "") or getattr(series, "id", "")):
        series_name = (
            await _tracked_listing_name("series", getattr(series, "id", "") or "")
            or series.name
            or ""
        )
        series_safe = _safe_name(
            series_name,
            fallback=_safe_name(series.id or "", fallback=_NO_SERIES_FOLDER),
        )
    else:
        series_safe = _NO_SERIES_FOLDER
    # 2026-07-20 alignment rule: the main 製作商 tree holds ONLY studios
    # the user tracks; everything else nests under the untracked sibling
    # so the folder count mirrors the tracked list. Tracking a studio
    # later flips this resolver immediately (upsert/untrack clear the
    # cache above); promoting the studio's EXISTING works = create the
    # main-tree target folders (POST /files/mkdir), then run a scoped
    # cleanup on its 其他製作商 folder — cleanup treats a missing target
    # as not-misplaced, so the mkdir comes first.
    base = (
        kind_base_path("studio")
        if is_tracked
        else untracked_studio_base_path()
    )
    return f"{base}/{studio_safe}/{series_safe}"


async def _studio_series_path(detail: MovieDetail, safe_code: str) -> str | None:
    """``<studio>/<series｜未分類>/<code>`` when the detail has a studio;
    ``None`` when it has no studio to nest under."""
    base = await _studio_series_dir(detail)
    return f"{base}/{safe_code}" if base is not None else None


async def studio_series_dir_for_code(
    code: str, *, allow_fetch: bool = True
) -> str | None:
    """Public helper: resolve a code's ``製作商/<studio>/<series>`` folder
    (no code leaf) from the detail cache, or ``None`` when it has no
    studio. Reused by the pCloud organizer to mirror the PikPak layout.
    ``allow_fetch=False`` keeps a cache miss from hitting JavBus."""
    detail = await _detail_for_archive(code, allow_fetch=allow_fetch)
    if detail is None:
        return None
    return await _studio_series_dir(detail)


async def _resolve_archive_path_by_code(code: str) -> str:
    """Primary path resolver: ``製作商/<studio>/<series>/<code>``.

    Every movie with a studio nests under the studio→series tree
    (regardless of which listing the user tracks). Movies with no studio
    fall back to the legacy single-kind layout (series/label/…) and,
    failing that, to ``pikpak_archive_folder`` (``AVBT/已完成``).

    Used by the archiver loop and by reorganize (which has no
    OfflineTaskLog row context)."""
    safe_code = _archive_leaf(code)
    detail = await _detail_for_archive(code)
    if detail is not None:
        nested = await _studio_series_path(detail, safe_code)
        if nested is not None:
            return nested
    # No studio → legacy single-kind fallback (unchanged behaviour).
    resolved = await resolve_listing_for_code(code)
    if resolved is None:
        return f"{settings.pikpak_archive_folder}/{safe_code}"
    kind, safe_name = resolved
    return f"{kind_base_path(kind)}/{safe_name}/{safe_code}"


async def _resolve_archive_path(row: OfflineTaskLog) -> str:
    """Pick the destination folder for ``row.code``.

    Delegates to :func:`_resolve_archive_path_by_code`. The old
    single-kind fast path (built from the ``tracked_*`` snapshot) is
    gone: the nested layout keys on the movie's own studio+series, and
    series is per-code — not knowable from the listing the download was
    triggered by — so it must come from the detail cache anyway. That
    read is a local DB hit (no HTTP) in the common case."""
    return await _resolve_archive_path_by_code(row.code)


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
            "cleanup_error_total": _cleanup_error_total,
            "finalize_concurrency": settings.archive_finalize_concurrency,
            "pcloud_poll_concurrency": settings.pcloud_poll_concurrency,
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

# Count of per-file "error" events phase-2 cleanup (rename/trash/move
# failures inside ``_cleanup_target_parents``) has yielded across every
# run. Cumulative, never reset — a status-page trend indicator, not a
# per-run count.
_cleanup_error_total: int = 0


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
        except Exception as exc:  # noqa: BLE001
            logger.warning("legacy sweep: presence cache invalidation failed: %s", exc)
        pikpak_service._folder_cache.clear()

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
    # Wrappers trashed as ad shells — their rows get orphaned instead.
    shell_ids: list[str] = []
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
                # Also log it — these used to vanish into the settings
                # page only, which hid weeks of failing sweeps.
                if not sweep_error:
                    logger.warning(
                        "root sweep phase1 error: %s", ev.get("message", "")
                    )
                sweep_error = sweep_error or ev.get("message", "")
                continue
            if ev_type != "progress":
                continue
            if ev.get("action") == "trash":
                sid = ev.get("source_id")
                if sid:
                    shell_ids.append(sid)
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
        # Junk that rode a wrapper into the flattened layout has no
        # owner: finalize only purges inside a 番號 folder, and the
        # retry pass just stamps an already-flattened code. Sweep it
        # here, on the same cleanup_all cadence (user-authorised
        # 2026-07-16 after 111 ad clips turned up library-wide).
        try:
            from .series_junk import purge_series_junk  # avoid cycle

            junk = await purge_series_junk(pikpak_service, dry_run=False)
            if junk.get("trashed"):
                logger.info(
                    "series junk sweep: trashed %d loose junk file(s)",
                    junk["trashed"],
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("series junk sweep failed: %s", exc)
        # Same gap, different leftover: a second magnet landing beside
        # CODE.mp4 becomes CODE(1).mp4, and nothing owned that either
        # (112 accumulated by 2026-07-16). Keeps the biggest — the
        # backfill only sends magnets ≥1.8x what we hold, so the newcomer
        # was always meant to replace the old copy — and trashes the rest.
        # User-authorised for the pipeline after reviewing the live run.
        try:
            from .dup_copies import sweep_dup_copies  # avoid cycle

            dups = await sweep_dup_copies(pikpak_service, dry_run=False)
            if dups.get("trashed") or dups.get("renamed"):
                logger.info(
                    "dup copies sweep: trashed %d, renamed %d",
                    dups.get("trashed", 0), dups.get("renamed", 0),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("dup copies sweep failed: %s", exc)

    # Stop the DB-driven pass from re-moving what we just moved. Without
    # this, every loop iteration retries the move (PikPak rejects it
    # because the file's no longer a child of AVBT root) and the log
    # fills with "move failed" warnings.
    if moved_ids:
        try:
            await _mark_offline_log_archived(moved_ids)
        except Exception as exc:  # noqa: BLE001
            logger.warning("offline log sync failed: %s", exc)

    if shell_ids:
        try:
            await _mark_offline_log_shell_trashed(shell_ids)
        except Exception as exc:  # noqa: BLE001
            logger.warning("offline log shell sync failed: %s", exc)
        webhook_queue.enqueue_nowait(
            f"🗑️ 掃描發現 {len(shell_ids)} 包廣告殼(整包無影片),已丟垃圾桶待換磁力",
            event="archive_done",
        )

    if moved:
        logger.info("root sweep moved %d orphan(s) to kind/name", moved)
        try:
            from . import missing as missing_svc  # avoid cycle
            await missing_svc.invalidate_all_caches_async(presence=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("root sweep: presence cache invalidation failed: %s", exc)
        # Folder cache may now point at trashed/renamed wrappers; drop
        # so the next folder_id() relists. reorganize_stream does the
        # same after its mutating runs.
        pikpak_service._folder_cache.clear()
    return moved


async def _flatten_swept_wrappers(
    wrappers: list[tuple[str, str, str, str]],
) -> int:
    """Rerun phase-2 flatten on each just-moved wrapper. Synthesises a
    PikPakFile stub from the post-move metadata — _resolve_folder_winner
    only reads .id / .name / .kind, so a fresh server fetch is
    unnecessary. Returns the count of wrappers actually flattened."""
    from ..schemas import PikPakFile
    from .reorganize import _resolve_folder_winner

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

    global _cleanup_error_total

    cleaned = 0
    for pid in parent_ids:
        try:
            children = await pikpak_service.list_files(pid, size=500)
            if not children:
                continue
            error_count = 0
            first_reason = ""
            async for ev in _phase2_cleanup_target(
                pid, pid, children, dry_run=False, idx_start=0
            ):
                if ev.get("action") == "error":
                    error_count += 1
                    if not first_reason:
                        first_reason = ev.get("reason") or ""
            if error_count:
                _cleanup_error_total += error_count
                logger.warning(
                    "phase-2 cleanup %s: %d error(s); first: %s",
                    pid, error_count, first_reason,
                )
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


async def _mark_offline_log_shell_trashed(file_ids: list[str]) -> None:
    """Orphan OfflineTaskLog rows whose wrapper the sweep trashed as an
    ad shell. Clearing ``file_id`` stops the DB-driven pass from trying
    to move a trashed id every minute, while ``archived`` stays False so
    the dead-code scan still sees an open row and the code gets re-sent
    with a different magnet instead of reading as archived success."""
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
            .values(
                file_id="",
                message="ad-shell wrapper trashed: no video/container inside",
            )
        )
        await session.commit()


# Finalize retry: only rows archived within this window. The per-pass
# cap is a runaway backstop, not a throughput throttle — a pass drains
# every eligible row (user directive 2026-07-15: "執行完再休息"; the old
# cap of 5/min queued for hours behind a mass auto-send). Hammering
# protection moved to the per-row failure cooldown + pass time budget
# below.
_FINALIZE_RETRY_WINDOW = timedelta(hours=24)
# The SQL limit must exceed any realistic eligible-row count: it slices
# BEFORE the in-loop active-task filter, so a cap below the count of
# newer still-downloading rows starves older landed rows out of the
# window entirely (live: MUDR-349 never selected behind a 500+ row
# auto-send flood — the same shape as the reaper starvation, #151).
_FINALIZE_RETRY_LIMIT = 2000
# A row that just failed (error / timeout) is skipped for this long so
# a block of failing rows costs one attempt each per cooldown, not one
# per 60s pass.
_FINALIZE_RETRY_COOLDOWN = timedelta(minutes=10)
_finalize_attempts: dict[int, datetime] = {}
# One pass must not monopolise the archiver loop: stop draining after
# this many seconds and let the next pass continue (the mover runs in
# the same loop, and fresh completions shouldn't wait behind a long
# finalize queue).
_FINALIZE_PASS_BUDGET = 900.0
# pikpakapi calls carry no timeout of their own; one hung mutation once
# froze the archiver loop permanently. Bound every finalize attempt and
# the presence walk so a stuck await costs one row / one pass, not the
# whole loop.
_FINALIZE_ROW_TIMEOUT = 300
_PRESENCE_TIMEOUT = 600
# Orphan reap only looks at rows from the current pipeline; older rows
# predate the finalized column and are not operationally pending.
_REAP_WINDOW = timedelta(days=7)
# Rows that failed the flattened check stay candidates (their code may
# land later), but re-checking them every pass would let a block of
# never-landing zombies (task gone, files never materialised) hog the
# per-pass cap forever and starve younger genuine orphans. Cooldown per
# row id; flattened state only changes when a sweep lands files, so a
# few hours of latency on the retry is free.
_REAP_RETRY_COOLDOWN = timedelta(hours=6)
# The reaper's flattened check costs live folder listings — keep its
# per-pass cap small and independent of the finalize drain above.
_REAP_CHECK_LIMIT = 5
_reap_attempts: dict[int, datetime] = {}

# A genuinely-dead orphan (no file ever landed) is dead-lettered instead
# of re-tried for the whole reap window, but only after this grace so a
# late-arriving download still gets its chance.
_ABANDON_GRACE = timedelta(hours=24)


async def _active_task_ids() -> set[str]:
    """Task ids that are still downloading (or otherwise not COMPLETE).

    The root sweep marks a wrapper ``archived`` the moment it moves it —
    which can happen while PikPak is still writing into it. Finalize
    permanently deletes files, so it must never run against a folder
    whose offline task hasn't finished; a half-downloaded second video
    can look exactly like a sub-300MB ad clip.

    PENDING must be asked for explicitly: pikpakapi's default filter is
    RUNNING+ERROR, so tasks queued behind PikPak's 100-task concurrency
    cap were invisible here — the retry pass then finalized their rows
    against the pre-upgrade files, and the reaper closed them as "task
    gone" while they were merely waiting in a 53-deep PENDING queue
    (live: MMGO-005, closed twice before its replacement ever ran)."""
    try:
        tasks = await pikpak_service.list_tasks(size=1000, phases=ACTIVE_PHASES)
    except Exception as exc:  # noqa: BLE001
        # Fail closed: with no task list we can't prove anything is
        # complete, so the caller skips this pass entirely.
        raise PikPakError(f"list_tasks unavailable: {exc}") from exc
    return {t.id for t in tasks if t.id and t.phase != "PHASE_TYPE_COMPLETE"}


async def _finalize_retry_pass() -> int:
    """Re-run finalize on recently-archived rows that missed it. Returns
    how many rows were finalized this pass."""
    from .finalize import run_finalize  # avoid cycle
    from .offline_tasks import SETTLE_GRACE  # avoid cycle

    cutoff = datetime.utcnow() - _FINALIZE_RETRY_WINDOW
    settle_cutoff = datetime.utcnow() - SETTLE_GRACE
    orphan_cutoff = datetime.utcnow() - _REAP_WINDOW
    done = 0
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(OfflineTaskLog)
                .where(
                    OfflineTaskLog.finalized.is_(False),
                    OfflineTaskLog.abandoned.is_(False),
                    OfflineTaskLog.superseded.is_(False),
                    or_(
                        # Normal path: sweep/archiver stamped the move.
                        (OfflineTaskLog.archived.is_(True))
                        & (OfflineTaskLog.archived_at > cutoff),
                        # Collecting-orphan path: file_id was empty at
                        # submit, so the sweep moved the wrapper but the
                        # stamp never matched — archived stays 0 and the
                        # wrapper sat in limbo forever (live: MUDR-349 /
                        # MUDR-358, wrappers full of junk 2h+ after
                        # landing). Rows whose task is still downloading
                        # are skipped in the loop via the active-task
                        # check; a task that's simply gone with nothing
                        # landed just no-ops into the failure cooldown.
                        (OfflineTaskLog.archived.is_(False))
                        & (OfflineTaskLog.created_at > orphan_cutoff),
                    ),
                    # Freshly-submitted tasks may still be materialising
                    # files — wait out the settle grace before the first
                    # destructive finalize (see services/offline_tasks).
                    OfflineTaskLog.created_at < settle_cutoff,
                    OfflineTaskLog.code != "",
                )
                # Oldest first: long-stuck wrappers are the visible
                # clutter; flood-fresh rows are usually still settling.
                .order_by(OfflineTaskLog.created_at.asc())
                .limit(_FINALIZE_RETRY_LIMIT)
            )
        ).scalars().all()
        if not rows:
            return 0
        try:
            active = await _active_task_ids()
        except PikPakError as exc:
            logger.warning("finalize retry skipped: %s", exc)
            return 0
        # Cheap DB-only filters first. The presence refresh below is
        # ~1-2 live PikPak listings per code, so refreshing a row this
        # pass will not touch is pure load: dead rows (task gone, nothing
        # landed, so the reaper's flattened check never owns them) stay
        # selected for the full _REAP_WINDOW, and refreshing every
        # selected row re-listed 281 codes every 60s pass to find ~1
        # change — sustained PikPak timeouts plus a minutes-long archiver
        # loop (live 2026-07-15). The cooldown below exists precisely to
        # stop that re-listing; it only works if it gates the refresh too.
        now = datetime.utcnow()
        candidates = []
        for row in rows:
            if row.task_id and row.task_id in active:
                continue  # still downloading — try again next pass
            last = _finalize_attempts.get(row.id)
            if last is not None and now - last < _FINALIZE_RETRY_COOLDOWN:
                continue  # just failed — let the cooldown expire first
            candidates.append(row)
        if not candidates:
            return 0
        # Both the presence-path folder fallback and the flattened check
        # read the presence index, and a snapshot from BEFORE the sweep
        # moved these wrappers only knows the old loose-file path — the
        # fallback then misses and the flattened check wrongly stamps the
        # row (observed live on DVDMS-306). Refresh just the codes this
        # pass will touch: one listing each, versus the full-drive walk
        # this used to force (10k codes, minutes, every stale pass).
        # Fail closed like the task-list guard.
        try:
            from .pikpak_presence import presence_index  # avoid cycle

            await asyncio.wait_for(
                presence_index.refresh_codes([r.code for r in candidates]),
                timeout=_PRESENCE_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("finalize retry skipped (presence): %s", exc)
            return 0
        pass_start = asyncio.get_event_loop().time()
        for row in candidates:
            if asyncio.get_event_loop().time() - pass_start > _FINALIZE_PASS_BUDGET:
                logger.info(
                    "finalize retry pass budget spent after %d rows; "
                    "resuming next pass", done,
                )
                break
            try:
                # One stuck PikPak mutation must not freeze the whole
                # archiver loop (a folder rename once hung it for good —
                # no timeout anywhere down the pikpakapi stack).
                async with asyncio.timeout(_FINALIZE_ROW_TIMEOUT):
                    # Shell-trash (#207) only for rows aged past the
                    # abandon grace. Age keys on when the wrapper was
                    # ARCHIVED (moved), not when the row was created: a
                    # PENDING task can sit >24h before its move lands, so
                    # created_at aging does not prove the folder settled
                    # (2026-07-18 audit). Collecting-orphans have no
                    # archived_at yet — fall back to created_at (their
                    # wrapper was migrated by the sweep long ago). The
                    # move-settle gate in finalize is the real guard;
                    # this just avoids opting in prematurely.
                    if await run_finalize(
                        pikpak_service, row.code,
                        allow_shell_trash=(
                            (row.archived_at or row.created_at)
                            < datetime.utcnow() - _ABANDON_GRACE
                        ),
                    ):
                        if not row.archived:
                            # Collecting-orphan: the sweep's stamp never
                            # matched, so close the move here too.
                            row.archived = True
                            row.archived_at = datetime.utcnow()
                        row.finalized = True
                        row.finalized_at = datetime.utcnow()
                        done += 1
                        _finalize_attempts.pop(row.id, None)
                    elif await _already_flattened(row.code):
                        # Sweep-archived rows use the flattened layout —
                        # the video sits directly in the 系列 folder, so
                        # there is no per-code folder to finalize. The
                        # sweep's own cleanup already normalised it.
                        if not row.archived:
                            row.archived = True
                            row.archived_at = datetime.utcnow()
                        row.finalized = True
                        row.finalized_at = datetime.utcnow()
                        done += 1
                        _finalize_attempts.pop(row.id, None)
                    else:
                        # Ran but not finalizable yet (settling / still
                        # materialising) — back off so the drain doesn't
                        # re-list the same folders every 60s.
                        _finalize_attempts[row.id] = datetime.utcnow()
            except TimeoutError:
                _finalize_attempts[row.id] = datetime.utcnow()
                logger.warning(
                    "finalize retry %s timed out after %ss",
                    row.code, _FINALIZE_ROW_TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001
                _finalize_attempts[row.id] = datetime.utcnow()
                logger.warning("finalize retry %s failed: %s", row.code, exc)
        if done:
            await session.commit()
    return done


async def _reap_orphan_rows() -> int:
    """Close rows the sweep's file_id stamp can never reach.

    A magnet submitted while still "Collecting" has no file_id yet; when
    the task then completes and drops off PikPak's task list between
    tracker polls, the row keeps ``file_id == ""`` forever. The sweep
    moves + flattens the wrapper fine, but ``_mark_offline_log_archived``
    matches rows by file_id, so the row sits at ``archived=0`` and never
    enters the finalize retry window (live case: MTM-013 2026-07-15 —
    files landed as ``_1/_2``, row permanently pending).

    The second path is a row that WAS archived but whose finalize never
    landed. ``_finalize_retry_pass`` selects on ``archived_at > cutoff``,
    so once a row ages past _FINALIZE_RETRY_WINDOW that pass drops it and
    — with the reaper historically requiring archived=False and no
    file_id — nothing owned it again. It stayed finalized=0 forever
    despite clean flattened files, permanently inflating the "not cleaned
    up" backlog the operator watches for new pipeline seams (live:
    300MIUM-1276/1277/1295/1299/1319, DVMM-380 — archived 07-12 while the
    pre-#160 drain was starved, still open at 80h).

    Only rows whose task is gone from the task list AND whose code is
    already flattened at the destination get stamped; anything still
    downloading, listed, or not yet landed keeps waiting. Pure DB
    bookkeeping — no file operations.

    The recency window keeps the reaper off the thousands of pre-2026-07
    historical rows (``finalized`` was backfilled as 0 when the column
    was added) — closing those would burn a flattened check (live folder
    listings) per row for zero operational gain. Within the window,
    still-listed tasks are skipped for free BEFORE the expensive-check
    cap is applied, so a burst of fresh Collecting submissions can't
    starve an older genuine orphan out of the pass (live near-miss:
    round-3 backfill submitted 51 Collecting rows minutes after the
    MTM-013 orphan appeared)."""
    from .offline_tasks import SETTLE_GRACE  # avoid cycle

    settle_cutoff = datetime.utcnow() - SETTLE_GRACE
    reap_cutoff = datetime.utcnow() - _REAP_WINDOW
    retry_cutoff = datetime.utcnow() - _FINALIZE_RETRY_WINDOW
    done = 0
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(OfflineTaskLog)
                .where(
                    OfflineTaskLog.finalized.is_(False),
                    OfflineTaskLog.abandoned.is_(False),
                    OfflineTaskLog.superseded.is_(False),
                    or_(
                        # Not-yet-archived orphan: the sweep's file_id
                        # stamp never owned this row. This spans BOTH the
                        # file_id-empty Collecting orphan (task vanished
                        # before a file_id was tracked) AND the
                        # file_id-nonempty stuck-"Saving" orphan (PikPak
                        # assigned a file_id but the download died and the
                        # task dropped off the list, so the stamp path,
                        # which matches on file_id, still never fired —
                        # archived stays 0). The finalize retry pass
                        # selects this whole population regardless of
                        # file_id, so the reaper must too: a file_id here
                        # is not evidence of a landed file, and requiring
                        # file_id=='' left ~96 dead "Saving" rows invisible
                        # to the reaper while the retry pass re-listed them
                        # every cooldown for the full reap window (live
                        # 2026-07-18: PA0-010/SNOS-257/GDTM-203, task gone
                        # 6-7d, presence empty, still churning 找不到).
                        # The per-row _orphan_has_nothing_landed gate below
                        # is what actually decides dead-vs-live, so this
                        # wider net is safe.
                        (OfflineTaskLog.archived.is_(False)),
                        # Archived but never finalized, and now past the
                        # retry pass's own lookback — that pass keys off
                        # archived_at > cutoff, so once a row ages out
                        # nothing else will ever close it. Inside the
                        # window the retry pass still owns it (it can run
                        # a real finalize and purge junk); after, this is
                        # the only backstop left.
                        (OfflineTaskLog.archived.is_(True))
                        & (OfflineTaskLog.archived_at < retry_cutoff),
                    ),
                    OfflineTaskLog.code != "",
                    OfflineTaskLog.created_at < settle_cutoff,
                    OfflineTaskLog.created_at > reap_cutoff,
                )
                .order_by(OfflineTaskLog.created_at.asc())
                .limit(500)
            )
        ).scalars().all()
        if not rows:
            return 0
        try:
            active = await _active_task_ids()
        except PikPakError as exc:
            logger.warning("orphan reap skipped: %s", exc)
            return 0
        # Drop stale attempt records so the map stays bounded.
        attempt_floor = datetime.utcnow() - _REAP_RETRY_COOLDOWN
        for rid in [k for k, v in _reap_attempts.items() if v < attempt_floor]:
            del _reap_attempts[rid]
        checked = 0
        abandoned = 0
        for row in rows:
            if row.task_id and row.task_id in active:
                continue  # still downloading — not an orphan
            if row.id in _reap_attempts:
                continue  # failed a recent check — let others have a slot
            if checked >= _REAP_CHECK_LIMIT:
                break  # cap the expensive flattened checks per pass
            checked += 1
            _reap_attempts[row.id] = datetime.utcnow()
            try:
                async with asyncio.timeout(_FINALIZE_ROW_TIMEOUT):
                    # Abandon is TERMINAL, so the flattened check must be a
                    # fresh, reliable negative — not a stale-snapshot or a
                    # swallowed-error False. Refresh this code's presence
                    # first (a pre-sweep snapshot only knows the old
                    # loose-file path — DVDMS-306; mirrors
                    # _finalize_retry_pass) and use strict=True so a
                    # transient check error raises into the except below
                    # (skip + cooldown) instead of abandoning a real row.
                    from .pikpak_presence import presence_index  # avoid cycle
                    await presence_index.refresh_codes([row.code])
                    if not await _already_flattened(row.code, strict=True):
                        # Not flattened, but that alone is NOT "dead": a
                        # per-code folder or task-wrapper files are also
                        # "not flattened" and still need finalize
                        # (MUDR-349/358, OYCVR-058). Only abandon when a
                        # positive check confirms the code has NOTHING on
                        # PikPak. strict=True → a check error raises into
                        # the except below (skip + cooldown), never abandons.
                        if (
                            not row.archived
                            and row.created_at
                            < datetime.utcnow() - _ABANDON_GRACE
                            and await _orphan_has_nothing_landed(
                                row.code, strict=True
                            )
                        ):
                            row.abandoned = True
                            # No file_id gate here: a stuck-"Saving" orphan
                            # keeps its stale file_id but the file it named
                            # is long gone (task vanished, nothing landed —
                            # _orphan_has_nothing_landed just confirmed it).
                            # "no archived copy found" not "nothing exists":
                            # for a file_id-empty collecting orphan whose
                            # video still sits in an un-moved download
                            # wrapper (no file_id to resolve it), the checks
                            # can't see it — the sweep still archives it
                            # independently; only this row's tracking is
                            # closed. Don't assert the stronger "nothing".
                            row.message = (
                                "abandoned: task gone, no archived copy found"
                            )
                            abandoned += 1
                            logger.info(
                                "orphan reap abandoned %s (task %s gone, "
                                "no archived copy found, >%dh old)",
                                row.code, row.task_id or "?",
                                int(_ABANDON_GRACE.total_seconds() // 3600),
                            )
                        continue  # not flattened → abandoned or needs finalize
            except TimeoutError:
                logger.warning(
                    "orphan reap %s timed out after %ss",
                    row.code, _FINALIZE_ROW_TIMEOUT,
                )
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("orphan reap %s failed: %s", row.code, exc)
                continue
            now = datetime.utcnow()
            was_archived = bool(row.archived)
            row.archived = True
            # Keep the original archive time on rows the sweep already
            # stamped — overwriting it would relabel 07-12 work as today's.
            if not was_archived:
                row.archived_at = now
            row.finalized = True
            row.finalized_at = now
            row.message = "auto-closed: task gone, files flattened"
            done += 1
            logger.warning(
                "orphan reap closed %s (task %s gone, %s; files already "
                "flattened)",
                row.code, row.task_id or "?",
                "archived but finalize never landed within the retry window"
                if was_archived else "vanished before file_id was tracked",
            )
        if done or abandoned:
            await session.commit()
    return done


async def _already_flattened(code: str, *, strict: bool = False) -> bool:
    """True when ``code``'s video exists on PikPak even though it has no
    per-code archive folder — the sweep's flatten put ``CODE.ext``
    straight into the 製作商/<studio>/<系列> folder. Nothing per-code is
    left for finalize to do, so the row can be marked finalized instead
    of spinning in the retry window."""
    from .finalize import presence_code_folders  # avoid cycle
    from .video_count import files_for_code  # avoid cycle

    try:
        # Only the missing-folder case qualifies. When the per-code
        # folder exists, run_finalize failed for a real reason (junk
        # still inside, move error, …) and must keep retrying.
        path = await _resolve_archive_path_by_code(code)
        if await pikpak_service.lookup_folder_id(path):
            return False
        # The sweep keeps a wrapper's BT name ([Thz.la]dvdms-129), so the
        # canonical path can miss while a per-code folder still exists —
        # that folder needs finalize, not a flattened stamp.
        if await presence_code_folders(pikpak_service, code):
            return False
        result = await files_for_code(code)
    except Exception as exc:  # noqa: BLE001
        logger.debug("flattened check %s failed: %s", code, exc)
        if strict:
            raise
        return False
    if not (result.get("ok") and result.get("files")):
        return False
    if result.get("source") == "task":
        # files_for_code fell back to the offline task's own file
        # listing — presence knows nothing about this code, so nothing
        # has been archived: the files still sit in the download
        # wrapper under their BT names, with bare-name paths the
        # dirty-parent cleanup below can't even resolve (live:
        # OYCVR-058, stamped finalized while fbfb.me@….part1-3.mp4 sat
        # unarchived in the task area). Not flattened — keep waiting
        # for the sweep.
        return False
    # The loose files exist, but their NAMES may still carry BT noise:
    # the sweep's phase-2 rename queue lives in memory only, so a
    # restart between phase-1 (move) and phase-2 (rename) leaves
    # ``dccdom.com@200GANA-3146.mp4`` sitting in the series folder —
    # and stamping finalized here made that permanent (live case
    # 2026-07-15: 56 files across ~40 codes after a deploy restart).
    # Run the same phase-2 cleanup on the parent folder(s) first; the
    # stamp still happens this pass — content is verified present, and
    # the cleanup is idempotent.
    await _cleanup_loose_parents_if_dirty(result["files"])
    return True


async def _orphan_has_nothing_landed(code: str, *, strict: bool = False) -> bool:
    """True only when ``code`` has NOTHING on PikPak: no per-code archive
    folder (canonical path or BT-named) and no video files anywhere (not
    flattened in the 系列 folder, not sitting in the download wrapper).

    This is the ONLY orphan state that is safe to dead-letter. A per-code
    folder (junk run_finalize cannot clean — MUDR-349/358) or files still
    in the task wrapper (OYCVR-058) mean real finalize work remains and the
    finalize retry pass must keep retrying them — abandoning would strand
    them terminally. Deliberately narrower than ``not _already_flattened``,
    which is also True for those needs-finalize states. (It re-does the
    same folder/file lookups as _already_flattened; kept separate for
    clarity — the duplicate listing only runs for an abandon candidate
    that already passed the cheap DB gates, at most _REAP_CHECK_LIMIT/pass.)

    ``strict`` re-raises a check error so the caller skips the row rather
    than making a terminal abandon decision on unreliable data."""
    from .finalize import presence_code_folders  # avoid cycle
    from .video_count import files_for_code  # avoid cycle

    try:
        path = await _resolve_archive_path_by_code(code)
        if await pikpak_service.lookup_folder_id(path):
            return False  # per-code folder exists → needs finalize
        if await presence_code_folders(pikpak_service, code):
            return False  # BT-named per-code folder → needs finalize
        result = await files_for_code(code)
    except Exception as exc:  # noqa: BLE001
        logger.debug("nothing-landed check %s failed: %s", code, exc)
        if strict:
            raise
        return False  # unknown → not confirmed-empty → don't abandon
    # Files anywhere (flattened OR in the task wrapper) → not nothing.
    return not (result.get("ok") and result.get("files"))


async def _cleanup_loose_parents_if_dirty(files: list[dict]) -> None:
    """Re-run phase-2 cleanup on the series folder(s) holding ``files``
    when any of them still needs a canonical rename. Best-effort: a
    failure here must never block the flattened stamp."""
    from .finalize import PART_MIN_BYTES  # avoid cycle
    from .jav_code import is_video
    from .rename_plan import _build_video_rename_plan  # avoid cycle

    try:
        names = {f.get("name") for f in files}
        parents: set[str] = set()
        for f in files:
            file_path, name = f.get("path") or "", f.get("name") or ""
            if file_path.endswith(f"/{name}"):
                parents.add(file_path[: -len(name) - 1])
        dirty_pids: set[str] = set()
        for parent in parents:
            pid = await pikpak_service.lookup_folder_id(parent)
            if not pid:
                continue
            children = await pikpak_service.list_files(pid, size=500)
            plan, _members = _build_video_rename_plan(
                children, PART_MIN_BYTES, is_video
            )
            if names & set(plan):
                dirty_pids.add(pid)
        if dirty_pids:
            cleaned = await _cleanup_target_parents(dirty_pids)
            logger.info(
                "flattened stamp: phase-2 rename was pending, cleaned "
                "%d folder(s)", cleaned,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("flattened canonical cleanup failed: %s", exc)


async def _run_finalize_batch(
    targets: dict[str, str], concurrency: int
) -> set[str]:
    """Finalize each DISTINCT code concurrently, bounded by ``concurrency``.

    ``targets`` maps code → its archive folder_id. Returns the set of codes
    whose finalize returned truthy. A per-code failure/timeout is isolated
    (returns that code as not-finalized) and never aborts the batch —
    mirroring the old inline per-row try/except. Workers take only
    primitives (code, folder_id); ``run_finalize`` touches no DB session,
    so this composes safely with a single-threaded caller session."""
    if not targets:
        return set()
    from .finalize import run_finalize  # avoid cycle

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(code: str, target_id: str) -> tuple[str, bool]:
        async with sem:
            try:
                async with asyncio.timeout(_FINALIZE_ROW_TIMEOUT):
                    ok = await run_finalize(
                        pikpak_service, code, folder_id=target_id
                    )
                return code, bool(ok)
            except Exception as exc:  # noqa: BLE001
                logger.warning("finalize %s failed: %s", code, exc)
                return code, False

    results = await asyncio.gather(
        *(_one(c, t) for c, t in targets.items()),
        return_exceptions=True,
    )
    return {r[0] for r in results if isinstance(r, tuple) and r[1]}


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

    # Retry finalize on recently-archived rows whose junk purge didn't
    # complete (e.g. the async PikPak move hadn't landed when the inline
    # attempt ran). Bounded + windowed so a permanently-odd folder can't
    # hammer PikPak forever; the manual detail-page button covers those.
    try:
        await _finalize_retry_pass()
    except Exception as exc:  # noqa: BLE001
        logger.warning("finalize retry pass failed: %s", exc)

    # Close rows whose task vanished before a file_id was ever tracked —
    # the sweep's file_id-based stamp can't reach them, so once the files
    # are verifiably flattened the row would otherwise pend forever.
    try:
        await _reap_orphan_rows()
    except Exception as exc:  # noqa: BLE001
        logger.warning("orphan row reap failed: %s", exc)

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
                    # Dead-lettered stuck-"Saving" rows keep a stale
                    # file_id (#203) — without this filter they hold
                    # pending > 0 forever and every pass burns a
                    # list_tasks round-trip for rows nothing will match.
                    OfflineTaskLog.abandoned.is_(False),
                    # Fossil rows reconciled by log_reconcile keep a stale
                    # file_id too — same reasoning as abandoned above.
                    OfflineTaskLog.superseded.is_(False),
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
        shell_trashed = 0
        moved_codes: list[str] = []
        finalize_targets: dict[str, str] = {}
        moved_rows_by_code: dict[str, list] = {}
        for row in rows:
            if not _safe_code(row.code):
                continue
            try:
                # An ad shell (files, zero video/container) must not be
                # archived — that mints a canonical-looking 番號 folder
                # nothing ever questions (live: EDD-138, OYC-205). Trash
                # it and orphan the row (file_id="") so this pass stops
                # matching it while the dead-code scan still sees an
                # unfinalized row and re-sends a different magnet.
                from .finalize import wrapper_is_ad_shell  # avoid cycle

                if await wrapper_is_ad_shell(pikpak_service, row.file_id):
                    await pikpak_service.trash_files([row.file_id])
                    row.file_id = ""
                    row.message = "ad-shell wrapper trashed: no video/container inside"
                    shell_trashed += 1
                    notifications.append(
                        f"🗑️ `{row.code}` 抓回整包無影片(廣告殼),已丟垃圾桶待換磁力"
                    )
                    logger.warning("ad shell trashed for %s (task %s)",
                                   row.code, row.task_id or "?")
                    continue
                target_path = await _resolve_archive_path(row)
                target_id = await pikpak_service.folder_id(target_path)
                if not target_id:
                    continue
                await pikpak_service.move_files([row.file_id], target_id)
                # The moved wrapper's listing is optimistic (#140): its
                # children can read as empty right after the move. Stamp
                # it so the aged retry pass's empty-shell trash cannot
                # take a wrapper whose files are still in flight.
                pikpak_service.record_move_source(row.file_id)
                row.archived = True
                row.archived_at = datetime.utcnow()
                moved += 1
                moved_codes.append(row.code)
                # Defer finalize to a bounded-concurrent batch after all
                # moves (Phase B). Record the target per DISTINCT code and
                # which successfully-moved rows share it, so Phase C flags
                # ONLY moved rows (a re-download's failed-move sibling must
                # not be marked finalized). run_finalize is best-effort;
                # the bounded retry pass / manual button cover misses.
                finalize_targets[row.code] = target_id
                moved_rows_by_code.setdefault(row.code, []).append(row)
                notifications.append(
                    f"📦 已歸檔 `{row.code}` ({row.name or row.file_id}) → `{target_path}`"
                )
                logger.info("archived %s -> %s", row.file_id, target_path)
            except Exception as exc:  # noqa: BLE001
                state.last_error = f"move {row.file_id} failed: {exc}"
                logger.warning("archive %s failed: %s", row.file_id, exc)
                # The loop retries every minute — notify only the first
                # failure per file so a stuck file can't spam the channel.
                if row.file_id not in _failure_notified:
                    _failure_notified.add(row.file_id)
                    webhook_queue.enqueue_nowait(
                        f"⚠️ 歸檔失敗 `{row.code}` ({row.name or row.file_id}): {exc}",
                        event="archive_failed",
                    )

        # Phase B: finalize each distinct moved code concurrently (bounded).
        # Serial moves above guarantee every file has been asked to land
        # before any finalize starts. Concurrent finalizes are safe even
        # when codes share a 系列 parent folder: finalize only WRITES
        # (moves keepers) into the parent, while every delete/trash and
        # every move-settle stamp is confined to the per-code subtree, so
        # no finalize ever mutates the shared parent destructively.
        finalized_codes = await _run_finalize_batch(
            finalize_targets, settings.archive_finalize_concurrency
        )
        # Phase C: back on the single caller session, flag finalized ONLY on
        # rows that actually moved (archived=True) for a finalized code.
        _now = datetime.utcnow()
        for code in finalized_codes:
            for row in moved_rows_by_code.get(code, []):
                row.finalized = True
                row.finalized_at = _now

        if moved or shell_trashed:
            await session.commit()
            # Newly-archived codes change which codes count as "present".
            # Update just those entries (one listing each) — the index is
            # persisted now, so a blanket invalidation would cost a full
            # drive walk on the next read for zero extra accuracy.
            try:
                from . import missing as missing_svc  # avoid cycle
                from .pikpak_presence import presence_index  # avoid cycle

                await presence_index.refresh_codes(moved_codes)
                missing_svc.invalidate_result_caches()
            except Exception as exc:  # noqa: BLE001
                logger.warning("archive: presence refresh failed: %s", exc)
            for msg in notifications:
                webhook_queue.enqueue_nowait(msg, event="archive_done")

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
