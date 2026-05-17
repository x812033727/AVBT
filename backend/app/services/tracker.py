"""Background tracker: periodically scans every TrackedListing for new
JavBus works (across stars, studios, labels, series, directors), fires
a webhook on detection, and optionally auto-submits them to PikPak."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from typing import Any

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import TrackedListing
from ..schemas import SendAllOptions
from ..scrapers import javbus as scraper
from . import missing as missing_svc
from .download_queue import Job, download_queue
from .notify import send_webhook
from .pikpak_presence import presence_index

logger = logging.getLogger(__name__)


_KIND_LABELS = {
    "star": "女優",
    "studio": "製作商",
    "label": "發行商",
    "series": "系列",
    "director": "導演",
}


class TrackerState:
    def __init__(self) -> None:
        self.enabled: bool = settings.tracker_enabled
        self.last_run: datetime | None = None
        self.last_error: str = ""
        self.last_new_total: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "interval_seconds": settings.tracker_interval_seconds,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "last_error": self.last_error,
            "last_new_total": self.last_new_total,
        }


state = TrackerState()


def _tracker_options() -> SendAllOptions:
    return SendAllOptions(
        hd_only=settings.tracker_auto_send_hd_only,
        skip_sent=settings.tracker_auto_send_skip_sent,
        prefer_max_size_mb=settings.tracker_auto_send_max_size_mb or None,
    )


async def _enqueue_auto_send(
    kind: str, slug: str, new_codes: list[str]
) -> int:
    """Combine ``new_codes`` (just-detected on page 1) with the listing's
    missing-from-PikPak codes (when the presence index is ready), dedupe,
    and push everything into the global download queue.

    Returns the number of codes enqueued. The queue itself coalesces
    duplicates that are already pending or in-flight, so calling this
    every tracker tick is safe — codes already being processed won't
    get re-submitted to PikPak."""
    combined: list[str] = list(new_codes)
    seen: set[str] = {c for c in combined if c}

    status = presence_index.status()
    # Only walk the JavBus catalog for the missing list when we have
    # trustworthy PikPak inventory data. Otherwise we'd mistake "no data"
    # for "nothing downloaded" and try to send the entire catalog.
    if status.get("ready") and not status.get("last_error"):
        try:
            result = await missing_svc.missing_for_listing(kind, slug)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "auto-send-missing %s/%s failed: %s", kind, slug, exc
            )
            result = None
        if result and result.total:
            for m in result.missing:
                if m.code and m.code not in seen:
                    seen.add(m.code)
                    combined.append(m.code)

    if not combined:
        return 0

    options = _tracker_options()
    source = f"tracker:{kind}:{slug}"
    for code in combined:
        await download_queue.enqueue(
            Job(code=code, options=options, source=source)
        )
    return len(combined)


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


async def check_listing(kind: str, listing_id: str) -> dict:
    """Check one tracked listing, update DB, notify and (optionally) auto-send."""
    async with SessionLocal() as session:
        row: TrackedListing | None = await session.get(TrackedListing, (kind, listing_id))
        if not row:
            return {"kind": kind, "id": listing_id, "error": "not found", "new_codes": []}

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
            return {
                "kind": actual_kind,
                "id": listing_id,
                "name": name,
                "error": row.last_error,
                "new_codes": [],
            }

        row.last_seen_code = new_last_seen
        row.last_checked_at = datetime.utcnow()
        if had_baseline and new_codes:
            row.new_count = (row.new_count or 0) + len(new_codes)
        await session.commit()

    if had_baseline and new_codes:
        label = _KIND_LABELS.get(kind, kind)
        msg = (
            f"🆕 {label} {name} 有 {len(new_codes)} 部新作品: "
            f"{', '.join(new_codes)}"
        )
        asyncio.create_task(send_webhook(msg))

    if auto_send:
        # One-pass enqueue:
        #   freshly-seen new_codes (cheap — just page 1)
        #   ∪ JavBus catalog codes still missing from PikPak (walks all
        #     listing pages, cached for 1h; only when presence index is
        #     ready)
        # Both sets go to the global download queue, which dedupes
        # in-flight, so the worker pool serialises submissions across
        # every listing rather than each listing firing its own burst.
        # We don't await — tracker stays responsive even when the
        # listing's backlog is hundreds of codes.
        fresh = new_codes if had_baseline else []
        asyncio.create_task(_enqueue_auto_send(actual_kind, slug, fresh))

    return {
        "kind": kind,
        "id": listing_id,
        "name": name,
        "new_codes": new_codes if had_baseline else [],
    }


async def check_all() -> list[dict]:
    async with SessionLocal() as session:
        pairs = (
            await session.execute(select(TrackedListing.kind, TrackedListing.id))
        ).all()

    results: list[dict] = []
    for kind, listing_id in pairs:
        try:
            results.append(await check_listing(kind, listing_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("check_listing %s/%s crashed: %s", kind, listing_id, exc)
            results.append(
                {"kind": kind, "id": listing_id, "error": str(exc), "new_codes": []}
            )
        await asyncio.sleep(1.0)
    return results


# Backwards-compat alias (services/archiver imports check_actress nowhere,
# but keep the symbol for anything that referenced it).
check_actress = check_listing


async def run_loop() -> None:
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
                missing_svc.invalidate_result_caches()
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
