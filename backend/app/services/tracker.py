"""Background tracker: periodically scans every TrackedListing for new
JavBus works (across stars, studios, labels, series, directors), fires
a webhook on detection, and optionally auto-submits them to PikPak."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import AppMeta, TrackedListing
from ..schemas import SendAllOptions
from ..scrapers import javbus as scraper
from . import missing as missing_svc
from .download_queue import Job, download_queue
from .pikpak_presence import presence_index
from .webhook_queue import webhook_queue

logger = logging.getLogger(__name__)


_KIND_LABELS = {
    "star": "女優",
    "studio": "製作商",
    "label": "發行商",
    "series": "系列",
    "director": "導演",
    "genre": "類別",
}

# Bounds the number of listings checked simultaneously. JavBus outbound
# HTTP is already serialised by the 1.2 s global throttle in
# ``scrapers/javbus.py`` — this only caps DB sessions and CPU-side
# parsing so a large tracked set doesn't slam SQLite with N=78 writers.
_CHECK_SEMAPHORE = asyncio.Semaphore(max(1, settings.tracker_check_concurrency))


class TrackerState:
    def __init__(self) -> None:
        # Seeded from the env default, then overridden by the operator's
        # stored choice — see ``load_persisted_toggles``.
        self.enabled: bool = settings.tracker_enabled
        # Gate for the historical-missing part of auto_send.
        self.backfill_enabled: bool = settings.tracker_backfill_enabled
        self.last_run: datetime | None = None
        self.last_error: str = ""
        self.last_new_total: int = 0
        # Live progress of the current ``check_all`` pass (background or
        # via the non-streaming ``/run-now`` endpoint). The frontend polls
        # ``/api/tracked/status`` so the user can see "background scan
        # X/Y: <listing>" even when no modal is open. The streaming
        # ``/run-now/stream`` path bypasses these fields — its own modal
        # surfaces progress directly.
        self.scan_in_progress: bool = False
        self.scan_current: int = 0
        self.scan_total: int = 0
        self.scan_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "backfill_enabled": self.backfill_enabled,
            "backfill_batch_limit": settings.tracker_backfill_batch_limit,
            "interval_seconds": settings.tracker_interval_seconds,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "last_error": self.last_error,
            "last_new_total": self.last_new_total,
            "scan_in_progress": self.scan_in_progress,
            "scan_current": self.scan_current,
            "scan_total": self.scan_total,
            "scan_name": self.scan_name,
        }


state = TrackerState()


def _tracker_options() -> SendAllOptions:
    return SendAllOptions(
        hd_only=settings.tracker_auto_send_hd_only,
        skip_sent=settings.tracker_auto_send_skip_sent,
        prefer_max_size_mb=settings.tracker_auto_send_max_size_mb or None,
    )


async def _enqueue_auto_send(
    kind: str, slug: str, new_codes: list[str], *, do_full_scan: bool = True
) -> int:
    """Combine ``new_codes`` (just-detected on page 1) with the listing's
    missing-from-PikPak codes (when the presence index is ready and
    ``do_full_scan`` is True), dedupe, and push everything into the
    global download queue.

    ``do_full_scan=False`` skips the JavBus catalog walk — caller uses
    this when the listing has been quiet long enough that re-walking
    every page is wasted work; only the freshly-detected ``new_codes``
    get enqueued.

    On a successful full scan, writes the resulting missing count to
    ``TrackedListing.last_missing_count`` + ``last_full_scan_at`` so the
    next ``_scan_due`` decision can skip listings that are now complete.

    Returns the number of codes enqueued. The queue itself coalesces
    duplicates that are already pending or in-flight, so calling this
    every tracker tick is safe — codes already being processed won't
    get re-submitted to PikPak."""
    combined: list[str] = list(new_codes)
    seen: set[str] = {c for c in combined if c}

    if do_full_scan:
        status = presence_index.status()
        # Only walk the JavBus catalog for the missing list when we have
        # trustworthy PikPak inventory data. Otherwise we'd mistake "no
        # data" for "nothing downloaded" and try to send the entire
        # catalog.
        if status.get("ready") and not status.get("last_error"):
            try:
                result = await missing_svc.missing_for_listing(kind, slug)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "auto-send-missing %s/%s failed: %s", kind, slug, exc
                )
                result = None
            if result and result.total and state.backfill_enabled:
                # Studios cover whole-maker catalogs — cap them tighter so
                # the first full scan after the series→studio cutover
                # doesn't flood the queue with thousands of codes at once.
                limit = (
                    settings.tracker_studio_backfill_batch_limit
                    if kind == "studio"
                    else settings.tracker_backfill_batch_limit
                )
                added = 0
                for m in result.missing:
                    if not m.code or m.code in seen:
                        continue
                    if limit > 0 and added >= limit:
                        logger.info(
                            "backfill %s/%s 截斷於 %d 筆(缺漏共 %d,其餘留給下輪)",
                            kind, slug, limit, len(result.missing),
                        )
                        break
                    seen.add(m.code)
                    combined.append(m.code)
                    added += 1
            if result is not None:
                # Even with backfill off, keep the dashboard count fresh.
                await _record_scan_result(kind, slug, len(result.missing))

    if not combined:
        return 0

    # Pull the tracked-listing snapshot once so every job we enqueue
    # carries the kind/slug/name the archiver needs to route the file
    # without a JavBus fetch_detail at archive time.
    tracked_name = ""
    async with SessionLocal() as session:
        row = await session.get(TrackedListing, (kind, slug))
        if row:
            tracked_name = row.name or ""

    options = _tracker_options()
    source = f"tracker:{kind}:{slug}"
    futures: list[asyncio.Future] = []
    for code in combined:
        futures.append(
            await download_queue.enqueue(
                Job(
                    code=code,
                    options=options,
                    source=source,
                    tracked_kind=kind,
                    tracked_slug=slug,
                    tracked_name=tracked_name,
                )
            )
        )
    # Fire-and-forget: jobs resolve as workers drain the queue, so the
    # failure roll-up must not block this tracker tick.
    asyncio.create_task(
        _report_auto_send_failures(kind, slug, tracked_name or slug, futures)
    )
    return len(combined)


async def _report_auto_send_failures(
    kind: str, slug: str, display_name: str, futures: list[asyncio.Future]
) -> None:
    """Await the enqueued jobs and surface failures — previously a
    tracker-submitted job that failed in the queue vanished without a
    trace unless the (default-off) download_failed event was on. Rolls
    failures up into ``TrackedListing.last_error`` (already rendered on
    the tracked page) and one summary notification per batch."""
    try:
        results = await asyncio.gather(*futures, return_exceptions=True)
    except asyncio.CancelledError:
        raise
    failed = [
        r for r in results
        if isinstance(r, BaseException)
        or getattr(r, "status", "") == "failed"
    ]
    if not failed:
        return
    summary = f"自動補檔 {len(failed)}/{len(results)} 筆失敗"
    logger.warning("tracker %s/%s: %s", kind, slug, summary)
    try:
        async with SessionLocal() as session:
            row = await session.get(TrackedListing, (kind, slug))
            if row is not None:
                row.last_error = summary
                await session.commit()
    except Exception:  # noqa: BLE001 — reporting must not crash anything
        logger.exception("記錄 last_error 失敗 %s/%s", kind, slug)
    webhook_queue.enqueue_nowait(
        f"⚠️ {display_name}:{summary}(詳見下載佇列 recent)",
        event="download_failed",
    )


async def _detect_new(
    kind: str, slug: str, uncensored: bool, last_seen: str
) -> tuple[list[str], str]:
    """Fetch page 1 and return (new_codes_in_order, new_last_seen)."""
    listing = await scraper.fetch_listing(kind, slug, page=1, uncensored=uncensored)
    top = [it.code for it in listing.items if it.code]
    if not top:
        return [], last_seen

    new_codes: list[str] = []
    if last_seen:
        for code in top:
            if code == last_seen:
                break
            new_codes.append(code)
    return new_codes, top[0]


# Daily cadence for listings that have nothing missing — no point
# walking JavBus every hour when there's nothing to enqueue and the
# page-1 check would just bump last_seen_code with no actionable result.
_DAILY_SECONDS = 86400


def _scan_due(row: TrackedListing) -> bool:
    """Tracker-tick skip rule: a listing whose last full scan found zero
    missing codes only needs revisiting once a day. Listings with a
    backlog (>=1 missing) run every tick as before. Listings that have
    never been scanned (no ``last_full_scan_at``) also run — we need at
    least one baseline before we can claim they're complete."""
    if row.last_missing_count == 0 and row.last_full_scan_at:
        if (datetime.utcnow() - row.last_full_scan_at).total_seconds() < _DAILY_SECONDS:
            return False
    return True


async def _record_scan_result(kind: str, slug: str, missing_count: int) -> None:
    """Persist the outcome of a completed full missing-scan so the next
    ``_scan_due`` check can short-circuit when nothing's missing."""
    async with SessionLocal() as session:
        row = await session.get(TrackedListing, (kind, slug))
        if row:
            row.last_missing_count = int(missing_count)
            row.last_full_scan_at = datetime.utcnow()
            await session.commit()


async def _record_missing_count(kind: str, slug: str) -> None:
    """Standalone missing-scan used by the manual ``force=True`` path on
    listings without ``auto_send``: walks the JavBus catalog, writes
    ``last_missing_count``, but does NOT enqueue anything to PikPak.
    Lets the user trigger a fresh count without flipping auto-send on.

    Raises on a failed catalog walk (JavBus 429/5xx/network) so the
    streaming "立即檢查" caller can report 失敗 instead of a silent 完成
    with a stale count. The fire-and-forget background path uses
    ``_record_missing_count_quiet`` to swallow + log instead."""
    result = await missing_svc.missing_for_listing(kind, slug, refresh=True)
    await _record_scan_result(kind, slug, len(result.missing))


async def _record_missing_count_quiet(kind: str, slug: str) -> None:
    """Fire-and-forget wrapper for the background path: logs and drops a
    failed missing-scan rather than surfacing an unretrieved task
    exception. The user-facing streaming path calls
    ``_record_missing_count`` directly so the failure can reach the UI."""
    try:
        await _record_missing_count(kind, slug)
    except Exception as exc:  # noqa: BLE001
        logger.warning("missing-count record %s/%s failed: %s", kind, slug, exc)


def _full_scan_due(row: TrackedListing) -> bool:
    """Adaptive scan policy: a quiet listing skips the JavBus walk, but
    we force one every ``tracker_quiet_skip_every`` ticks regardless so
    a backfilled-earlier code can't slip past forever."""
    threshold = max(0, settings.tracker_quiet_skip_threshold)
    quiet = int(row.quiet_ticks or 0)
    if quiet < threshold:
        return True
    last_full = row.last_full_scan_at
    if last_full is None:
        return True
    every = max(1, settings.tracker_quiet_skip_every)
    interval = max(60, settings.tracker_interval_seconds)
    if datetime.utcnow() - last_full >= timedelta(seconds=every * interval):
        return True
    return False


class _Phase1Result(NamedTuple):
    """Outcome of the page-1 detection phase: enough state for both the
    fire-and-forget background path (``check_listing``) and the inline
    streaming path (``check_listing_stream``) to decide what comes next
    (webhook, auto-send, missing-scan)."""
    name: str
    auto_send: bool
    had_baseline: bool
    new_codes: list[str]
    do_full_scan: bool
    error: str | None
    not_found: bool


async def _check_listing_phase1(
    kind: str, listing_id: str, *, force: bool = False
) -> _Phase1Result:
    """Shared front-half of ``check_listing``: open the DB row, run the
    page-1 JavBus check, persist last_seen / quiet_ticks / last_full_scan_at.
    Does NOT enqueue anything or fire webhooks — that's the caller's job
    so the streaming variant can yield phase events around it."""
    async with SessionLocal() as session:
        row: TrackedListing | None = await session.get(
            TrackedListing, (kind, listing_id)
        )
        if not row:
            return _Phase1Result(
                name=listing_id, auto_send=False, had_baseline=False,
                new_codes=[], do_full_scan=False,
                error="not found", not_found=True,
            )

        slug = row.id
        actual_kind = row.kind
        uncensored = bool(row.uncensored)
        last_seen = row.last_seen_code
        name = row.name or slug
        auto_send = bool(row.auto_send)
        had_baseline = bool(last_seen)

        try:
            new_codes, new_last_seen = await _detect_new(
                actual_kind, slug, uncensored, last_seen
            )
            row.last_error = ""
        except Exception as exc:  # noqa: BLE001
            row.last_error = str(exc)[:500]
            row.last_checked_at = datetime.utcnow()
            await session.commit()
            return _Phase1Result(
                name=name, auto_send=auto_send, had_baseline=had_baseline,
                new_codes=[], do_full_scan=False,
                error=row.last_error, not_found=False,
            )

        row.last_seen_code = new_last_seen
        row.last_checked_at = datetime.utcnow()
        if had_baseline and new_codes:
            row.new_count = (row.new_count or 0) + len(new_codes)
            row.quiet_ticks = 0
        else:
            row.quiet_ticks = int(row.quiet_ticks or 0) + 1

        do_full_scan = force or (_full_scan_due(row) if auto_send else False)
        if do_full_scan:
            row.last_full_scan_at = datetime.utcnow()
        await session.commit()

    return _Phase1Result(
        name=name, auto_send=auto_send, had_baseline=had_baseline,
        new_codes=new_codes, do_full_scan=do_full_scan,
        error=None, not_found=False,
    )


def _maybe_fire_new_codes_webhook(
    kind: str, name: str, new_codes: list[str]
) -> None:
    """Fire the ``🆕 …`` webhook for newly-detected codes. Caller has
    already filtered for had_baseline + non-empty new_codes."""
    label = _KIND_LABELS.get(kind, kind)
    msg = (
        f"🆕 {label} {name} 有 {len(new_codes)} 部新作品: "
        f"{', '.join(new_codes)}"
    )
    webhook_queue.enqueue_nowait(msg, event="tracked_new")


async def _read_row_snapshot(kind: str, slug: str) -> dict:
    """Return the post-scan row state needed by the frontend to patch
    its local ``items`` / ``missing`` Maps without re-fetching
    ``/api/tracked``. Empty dict when the row no longer exists.

    All four "scan-side-effect" columns plus the missing count are read
    in a single SELECT, after whichever phase committed them (page-1
    write inside ``_check_listing_phase1`` or the missing-scan write
    inside ``_enqueue_auto_send`` / ``_record_scan_result``)."""
    async with SessionLocal() as session:
        row = await session.get(TrackedListing, (kind, slug))
        if row is None:
            return {}
        return {
            "last_checked_at": (
                row.last_checked_at.isoformat() + "Z"
                if row.last_checked_at else None
            ),
            "last_seen_code": row.last_seen_code or "",
            "new_count": int(row.new_count or 0),
            "last_error": row.last_error or "",
            "missing_count": int(row.last_missing_count or 0),
        }


async def check_listing(
    kind: str, listing_id: str, *, force: bool = False
) -> dict:
    """Check one tracked listing, update DB, notify and (optionally) auto-send.

    ``force=True`` bypasses ``_full_scan_due``'s adaptive skip so a manual
    "立即檢查" always walks the JavBus catalog for the missing list. For
    non-auto_send listings, that walk still happens (via
    ``_record_missing_count``) so the user gets a refreshed missing
    count even when the listing isn't auto-downloading.

    Fire-and-forget on the missing scan: returns as soon as page-1 is
    done so the background tracker loop stays responsive even when a
    listing's backlog is hundreds of codes. ``check_listing_stream`` is
    the inline-await variant used by streaming UI endpoints."""
    p = await _check_listing_phase1(kind, listing_id, force=force)
    if p.not_found:
        return {"kind": kind, "id": listing_id, "error": "not found", "new_codes": []}
    if p.error:
        return {
            "kind": kind, "id": listing_id, "name": p.name,
            "error": p.error, "new_codes": [],
        }

    if p.had_baseline and p.new_codes:
        _maybe_fire_new_codes_webhook(kind, p.name, p.new_codes)

    if p.auto_send:
        # One-pass enqueue:
        #   freshly-seen new_codes (cheap — just page 1)
        #   ∪ JavBus catalog codes still missing from PikPak (walks all
        #     listing pages, cached for 1h; only when presence index is
        #     ready AND adaptive policy says to scan this tick)
        # Both sets go to the global download queue, which dedupes
        # in-flight, so the worker pool serialises submissions across
        # every listing rather than each listing firing its own burst.
        # We don't await — tracker stays responsive even when the
        # listing's backlog is hundreds of codes.
        fresh = p.new_codes if p.had_baseline else []
        asyncio.create_task(
            _enqueue_auto_send(kind, listing_id, fresh, do_full_scan=p.do_full_scan)
        )
    elif force:
        # Non-auto_send + manual force: still refresh the missing count
        # so the next _scan_due decision sees an accurate baseline,
        # without sending anything to PikPak.
        asyncio.create_task(_record_missing_count_quiet(kind, listing_id))

    return {
        "kind": kind,
        "id": listing_id,
        "name": p.name,
        "new_codes": p.new_codes if p.had_baseline else [],
    }


async def check_listing_stream(
    kind: str, listing_id: str, *, force: bool = True
) -> AsyncIterator[dict]:
    """Streaming variant of ``check_listing``: yields ``start`` /
    ``progress`` / ``done`` events around the same phases, and awaits
    the missing scan inline (no fire-and-forget) so the caller sees a
    ``done`` event only after every phase actually completes.

    Used by the manual "立即檢查" / "全部立即檢查" endpoints where the
    user is watching progress on the UI; the background tracker loop
    still uses ``check_listing`` for responsiveness."""
    yield {"type": "start", "kind": kind, "id": listing_id}

    yield {"type": "progress", "phase": "page1",
           "message": "檢查 JavBus 第 1 頁…"}
    p = await _check_listing_phase1(kind, listing_id, force=force)

    if p.not_found:
        # No row to read — empty snapshot, frontend skips patch.
        yield {"type": "done", "kind": kind, "id": listing_id,
               "name": listing_id, "new_codes": [],
               "error": "not found"}
        return
    if p.error:
        # Phase-1 committed last_error / last_checked_at; surface the
        # snapshot so the row shows the failure inline.
        snapshot = await _read_row_snapshot(kind, listing_id)
        yield {"type": "done", "kind": kind, "id": listing_id,
               "name": p.name, "new_codes": [],
               "error": p.error, **snapshot}
        return

    if p.had_baseline and p.new_codes:
        _maybe_fire_new_codes_webhook(kind, p.name, p.new_codes)

    if p.auto_send:
        fresh = p.new_codes if p.had_baseline else []
        if p.do_full_scan:
            yield {"type": "progress", "phase": "missing_scan",
                   "message": "走 JavBus catalog…"}
        try:
            queued = await _enqueue_auto_send(
                kind, listing_id, fresh, do_full_scan=p.do_full_scan
            )
        except Exception as exc:  # noqa: BLE001
            snapshot = await _read_row_snapshot(kind, listing_id)
            yield {"type": "done", "kind": kind, "id": listing_id,
                   "name": p.name, "new_codes": fresh,
                   "error": str(exc), **snapshot}
            return
        yield {"type": "progress", "phase": "enqueue", "queued": queued,
               "message": f"已送 {queued} 個進下載佇列"}
    elif force:
        yield {"type": "progress", "phase": "missing_scan",
               "message": "重算缺漏…"}
        try:
            await _record_missing_count(kind, listing_id)
        except Exception as exc:  # noqa: BLE001
            # JavBus 429/5xx/network during the catalog walk. Report 失敗
            # instead of yielding a clean done with a stale count — the
            # whole point of this fix.
            snapshot = await _read_row_snapshot(kind, listing_id)
            yield {"type": "done", "kind": kind, "id": listing_id,
                   "name": p.name,
                   "new_codes": p.new_codes if p.had_baseline else [],
                   "error": str(exc), **snapshot}
            return

    snapshot = await _read_row_snapshot(kind, listing_id)
    yield {"type": "done", "kind": kind, "id": listing_id,
           "name": p.name,
           "new_codes": p.new_codes if p.had_baseline else [],
           "error": None, **snapshot}


async def _check_listing_complete(
    kind: str, listing_id: str, *, force: bool = False
) -> dict:
    """Inline-await variant for ``check_all_stream``: drives
    ``check_listing_stream`` to completion and returns the ``done`` event
    payload. Per-listing exceptions are flattened so callers don't need
    a try/except around each one."""
    result: dict = {"kind": kind, "id": listing_id, "new_codes": []}
    try:
        async for ev in check_listing_stream(kind, listing_id, force=force):
            if ev.get("type") == "done":
                result = {k: v for k, v in ev.items() if k != "type"}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "check_listing_stream %s/%s crashed: %s",
            kind, listing_id, exc,
        )
        result = {"kind": kind, "id": listing_id, "error": str(exc), "new_codes": []}
    return result


async def _guarded_check(
    kind: str, listing_id: str, *, force: bool = False
) -> dict:
    """Semaphore-bounded wrapper around ``check_listing`` so a large
    tracked set doesn't open N concurrent SQLite sessions. JavBus
    outbound HTTP is already serialised globally — this is a CPU + DB
    concurrency cap, not a network one. Per-listing exceptions are
    flattened into the same shape ``check_listing`` returns on error so
    callers don't need a try/except around each one."""
    async with _CHECK_SEMAPHORE:
        try:
            return await check_listing(kind, listing_id, force=force)
        except Exception as exc:  # noqa: BLE001
            logger.warning("check_listing %s/%s crashed: %s", kind, listing_id, exc)
            return {"kind": kind, "id": listing_id, "error": str(exc), "new_codes": []}


async def check_all(*, force: bool = False) -> list[dict]:
    """Run a tracker pass over every TrackedListing row.

    ``force=False`` (the background loop): listings whose last full scan
    found zero missing get skipped until 24h have elapsed since that
    scan. ``force=True`` (the manual non-streaming "全部立即檢查"): no
    skipping; every listing is checked and gets a fresh missing-count
    scan.

    Consumes ``check_all_stream`` so the same per-listing concurrency +
    cancellation logic runs in both code paths and the state fields
    (``scan_in_progress`` etc.) get updated as each listing completes.
    The streaming endpoint surfaces these events directly to the UI;
    the background loop relies on this status update for the inline
    progress banner."""
    state.scan_in_progress = True
    state.scan_current = 0
    state.scan_total = 0
    state.scan_name = ""
    results: list[dict] = []
    try:
        async for ev in check_all_stream(force=force):
            ev_type = ev.get("type")
            if ev_type == "start":
                state.scan_total = int(ev.get("total", 0) or 0)
                state.scan_current = 0
                state.scan_name = ""
            elif ev_type == "progress":
                state.scan_current = int(ev.get("current", 0) or 0)
                state.scan_name = ev.get("name") or ""
                results.append({k: v for k, v in ev.items() if k != "type"})
    finally:
        state.scan_in_progress = False
        state.scan_current = 0
        state.scan_total = 0
        state.scan_name = ""
    return results


async def _guarded_check_complete(
    kind: str, listing_id: str, *, force: bool = False
) -> dict:
    """Semaphore-bounded wrapper around ``_check_listing_complete`` —
    the inline-await variant used by the streaming batch path so each
    listing's full work (page-1 + missing scan + enqueue) is awaited
    before being reported as ``progress`` on the stream."""
    async with _CHECK_SEMAPHORE:
        return await _check_listing_complete(kind, listing_id, force=force)


async def check_all_stream(*, force: bool = True) -> AsyncIterator[dict]:
    """Streaming variant of ``check_all`` for the "全部立即檢查" button.

    Same row selection / skip rules as ``check_all``, but yields a
    ``start`` event followed by one ``progress`` event per listing as
    each completes (using ``asyncio.as_completed`` so the user sees
    incremental progress even though tasks run concurrently behind the
    ``_CHECK_SEMAPHORE``)."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(TrackedListing))
        ).scalars().all()

    pairs: list[tuple[str, str]] = []
    for r in rows:
        if force or _scan_due(r):
            pairs.append((r.kind, r.id))
    skipped = len(rows) - len(pairs)

    yield {
        "type": "start",
        "total": len(pairs),
        "skipped": skipped,
        "all": len(rows),
    }
    if not pairs:
        yield {"type": "done", "total": 0, "errors": 0}
        return

    tasks = [
        asyncio.create_task(
            _guarded_check_complete(kind, listing_id, force=force)
        )
        for kind, listing_id in pairs
    ]
    idx = 0
    errors = 0
    try:
        for coro in asyncio.as_completed(tasks):
            result = await coro
            idx += 1
            if result.get("error"):
                errors += 1
            yield {
                "type": "progress",
                "current": idx,
                "total": len(pairs),
                **result,
            }
    finally:
        # Client disconnect / generator close cancels the stream — cancel
        # any still-running per-listing tasks so we don't leak background
        # JavBus crawls.
        for t in tasks:
            if not t.done():
                t.cancel()

    yield {"type": "done", "total": len(pairs), "errors": errors}


# Backwards-compat alias (services/archiver imports check_actress nowhere,
# but keep the symbol for anything that referenced it).
check_actress = check_listing


# Runtime toggles persist in AppMeta, the same way the notify switches
# do (CLAUDE.md: 執行期可調的開關存 app_meta). Memory alone meant every
# restart silently reverted the operator's choice to the env default —
# both default True, so a deploy re-enabled an auto-send that had been
# deliberately turned off and the first run_loop iteration walked all
# 47 tracked listings straight into JavBus 429 backoff (live 2026-07-15).
_TOGGLE_KEYS: dict[str, str] = {
    "enabled": "tracker:enabled",
    "backfill_enabled": "tracker:backfill_enabled",
}


async def set_toggle(name: str, enabled: bool) -> None:
    """Flip a runtime toggle and remember it across restarts."""
    key = _TOGGLE_KEYS[name]
    setattr(state, name, enabled)
    async with SessionLocal() as session:
        row = await session.get(AppMeta, key)
        if row is None:
            row = AppMeta(key=key)
            session.add(row)
        row.value = "1" if enabled else "0"
        await session.commit()


async def load_persisted_toggles() -> None:
    """Apply stored toggles over the env defaults at startup. A missing
    row means the operator never touched that switch — keep the default
    rather than inventing one. Never fatal: a toggle we can't read just
    leaves the env default in place."""
    try:
        async with SessionLocal() as session:
            for name, key in _TOGGLE_KEYS.items():
                row = await session.get(AppMeta, key)
                if row is not None and row.value in ("0", "1"):
                    setattr(state, name, row.value == "1")
                    logger.info(
                        "tracker %s restored from app_meta: %s",
                        name, row.value == "1",
                    )
    except Exception as exc:  # noqa: BLE001
        logger.warning("tracker toggle load failed: %s", exc)


async def run_loop() -> None:
    # Before the first check_all: an un-restored toggle would let a
    # disabled tracker scan once on every restart.
    await load_persisted_toggles()
    consecutive_errors = 0
    while True:
        try:
            if state.enabled:
                results = await check_all()
                state.last_new_total = sum(len(r.get("new_codes") or []) for r in results)
                state.last_error = ""
                # A scheduled batch just touched every listing's
                # last_seen_code and may have enqueued downloads —
                # drop the cached missing-summary so the next /tracked
                # page load reflects the new state.
                missing_svc.invalidate_all_caches()
            consecutive_errors = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            consecutive_errors += 1
            state.last_error = str(exc)
            logger.exception("tracker loop iteration failed")
        finally:
            state.last_run = datetime.utcnow()
        base = max(60, settings.tracker_interval_seconds)
        backoff = min(4, 2 ** consecutive_errors) if consecutive_errors else 1
        jitter = random.uniform(0, min(60, base / 10))
        await asyncio.sleep(base * backoff + jitter)
