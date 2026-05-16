"""Background tracker: periodically scans every TrackedActress for new
JavBus works, fires a webhook on detection, and optionally auto-submits
them to PikPak."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from typing import Any

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import TrackedActress
from ..schemas import SendAllOptions
from ..scrapers import javbus as scraper
from .bulk import send_codes_stream
from .notify import send_webhook

logger = logging.getLogger(__name__)


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


async def _auto_send(codes: list[str]) -> None:
    """Submit codes to PikPak via the standard bulk pipeline."""
    if not codes:
        return
    options = SendAllOptions(
        hd_only=settings.tracker_auto_send_hd_only,
        skip_sent=settings.tracker_auto_send_skip_sent,
    )
    try:
        async for _event in send_codes_stream(codes, options):
            pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("tracker auto-send failed: %s", exc)


async def _detect_new(slug: str, uncensored: bool, last_seen: str) -> tuple[list[str], str]:
    """Fetch page 1 and return (new_codes_in_order, new_last_seen)."""
    listing = await scraper.fetch_star(slug, page=1, uncensored=uncensored)
    top = [it.code for it in listing.items if it.code]
    if not top:
        return [], last_seen

    new_codes: list[str] = []
    if last_seen:
        for code in top:
            if code == last_seen:
                break
            new_codes.append(code)
    # else: first run, treat current top as baseline (no notifications)
    return new_codes, top[0]


async def check_actress(actress_id: str) -> dict:
    """Check one actress, update DB, notify and (optionally) auto-send.
    Returns a small result dict (id, name, new_codes, error)."""
    async with SessionLocal() as session:
        row: TrackedActress | None = await session.get(TrackedActress, actress_id)
        if not row:
            return {"id": actress_id, "error": "not found", "new_codes": []}

        slug = row.id
        uncensored = bool(row.uncensored)
        last_seen = row.last_seen_code
        name = row.name or slug
        auto_send = bool(row.auto_send)
        had_baseline = bool(last_seen)

        try:
            new_codes, new_last_seen = await _detect_new(slug, uncensored, last_seen)
            row.last_error = ""
        except Exception as exc:  # noqa: BLE001
            row.last_error = str(exc)[:500]
            row.last_checked_at = datetime.utcnow()
            await session.commit()
            return {"id": actress_id, "name": name, "error": row.last_error, "new_codes": []}

        row.last_seen_code = new_last_seen
        row.last_checked_at = datetime.utcnow()
        if had_baseline and new_codes:
            row.new_count = (row.new_count or 0) + len(new_codes)
        await session.commit()

    # Outside DB session: side-effects
    if had_baseline and new_codes:
        msg = f"🆕 {name} 有 {len(new_codes)} 部新作品: {', '.join(new_codes)}"
        asyncio.create_task(send_webhook(msg))
        if auto_send:
            asyncio.create_task(_auto_send(new_codes))

    return {"id": actress_id, "name": name, "new_codes": new_codes if had_baseline else []}


async def check_all() -> list[dict]:
    async with SessionLocal() as session:
        ids = (await session.execute(select(TrackedActress.id))).scalars().all()

    results: list[dict] = []
    for actress_id in ids:
        try:
            results.append(await check_actress(actress_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("check_actress %s crashed: %s", actress_id, exc)
            results.append({"id": actress_id, "error": str(exc), "new_codes": []})
        # tiny gap between actresses to avoid hammering the site
        await asyncio.sleep(1.0)
    return results


async def run_loop() -> None:
    while True:
        try:
            if state.enabled:
                results = await check_all()
                state.last_new_total = sum(len(r.get("new_codes") or []) for r in results)
                state.last_error = ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            state.last_error = str(exc)
            logger.exception("tracker loop iteration failed")
        finally:
            state.last_run = datetime.utcnow()
        await asyncio.sleep(max(60, settings.tracker_interval_seconds))
