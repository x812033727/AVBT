"""Tracked-page "存到 Google Drive": keep Drive-enabled listings moved.

Every ``gdrive_auto_interval_minutes`` (and right away when a listing is
switched on) ``reconcile`` looks at each listing with
``tracked_listing.gdrive_enabled`` and creates an ``origin="auto"`` job
for its downloaded works that are not on Drive yet. That covers both the
works it already had and everything archived later.

A freshly archived work is left alone until it has settled: its
OfflineTaskLog row (if any) is finalized, and its presence entry is older
than ``gdrive_auto_settle_minutes`` — otherwise the move would race the
archiver's finalize pass (keep-only-videos purge, renames).

Traffic budget / time window / per-account cap stay with the transfer
worker; this module only creates jobs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import (
    GDriveJob,
    GDriveLocation,
    GDriveTransfer,
    OfflineTaskLog,
    PresenceEntry,
    TrackedListing,
)
from . import gdrive_accounts, listing_codes
from .gdrive_transfer import ACTIVE_JOB_STATES, gdrive_transfer_queue, pikpak_unavailable
from .jav_code import normalize_code

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
_ERROR_BACKOFF_MINUTES = 60


def _norm(code: str) -> str:
    return normalize_code(code) or (code or "").upper()


async def _unsettled(codes: set[str]) -> set[str]:
    """Codes still being archived/finalized or that landed too recently."""
    if not codes:
        return set()
    cutoff = datetime.utcnow() - timedelta(minutes=max(0, settings.gdrive_auto_settle_minutes))
    async with SessionLocal() as session:
        pending_logs = (
            await session.execute(
                select(OfflineTaskLog.code).where(
                    OfflineTaskLog.archived.is_(True),
                    OfflineTaskLog.finalized.is_(False),
                    OfflineTaskLog.abandoned.is_(False),
                )
            )
        ).scalars().all()
        recent = (
            await session.execute(
                select(PresenceEntry.code).where(PresenceEntry.updated_at > cutoff)
            )
        ).scalars().all()
    busy = {_norm(c) for c in pending_logs} | {_norm(c) for c in recent}
    return codes & busy


async def _already_handled(codes: set[str]) -> set[str]:
    """Codes on Drive (any account), queued in an active transfer, or
    named by another active job that hasn't finished scanning yet."""
    if not codes:
        return set()
    async with SessionLocal() as session:
        on_drive = (await session.execute(select(GDriveLocation.code).distinct())).scalars().all()
        queued = (
            await session.execute(
                select(GDriveTransfer.code).where(GDriveTransfer.status.in_(("pending", "running")))
            )
        ).scalars().all()
        job_codes = (
            await session.execute(
                select(GDriveJob.codes).where(
                    GDriveJob.status.in_(ACTIVE_JOB_STATES), GDriveJob.codes != ""
                )
            )
        ).scalars().all()
    taken = {_norm(c) for c in on_drive} | {_norm(c) for c in queued}
    for raw in job_codes:
        try:
            taken |= {_norm(c) for c in json.loads(raw)}
        except ValueError:
            continue
    return codes & taken


async def _set_error(kind: str, listing_id: str, message: str) -> None:
    async with SessionLocal() as session:
        row = await session.get(TrackedListing, (kind, listing_id))
        if row is not None and row.gdrive_last_error != message:
            row.gdrive_last_error = message[:1000]
            await session.commit()


async def reconcile_listing(row: TrackedListing) -> int | None:
    """Create an auto job for ``row`` if it has unmoved, settled works.
    Returns the new job id (or None)."""
    kind, lid = row.kind, row.id
    try:
        await gdrive_accounts.credentials(row.gdrive_account_id)
    except gdrive_accounts.AccountUnavailable as exc:
        await _set_error(kind, lid, str(exc))
        return None

    async with SessionLocal() as session:
        active = (
            await session.execute(
                select(GDriveJob.id).where(
                    GDriveJob.listing_kind == kind,
                    GDriveJob.studio_id == lid,
                    GDriveJob.status.in_(ACTIVE_JOB_STATES),
                )
            )
        ).first()
    if active:
        return None

    # Back off after a failed scan (PikPak logged out / down): don't pile
    # up one error job per cycle; show the reason on the tracked row.
    async with SessionLocal() as session:
        last = (
            await session.execute(
                select(GDriveJob.status, GDriveJob.message, GDriveJob.finished_at)
                .where(GDriveJob.listing_kind == kind, GDriveJob.studio_id == lid)
                .order_by(GDriveJob.id.desc())
                .limit(1)
            )
        ).first()
    if (
        last is not None
        and last.status == "error"
        and last.finished_at is not None
        and datetime.utcnow() - last.finished_at < timedelta(minutes=_ERROR_BACKOFF_MINUTES)
    ):
        await _set_error(kind, lid, f"上次搬移失敗,稍後自動重試: {last.message}")
        return None

    codes = await listing_codes.codes_for_listing(kind, lid)
    todo = codes - await _already_handled(codes)
    todo -= await _unsettled(todo)
    await _set_error(kind, lid, "")
    if not todo:
        return None
    results = await gdrive_transfer_queue.create_jobs(
        [(kind, lid, row.name or lid)],
        account_id=row.gdrive_account_id,
        delete_source=bool(row.gdrive_delete_source),
        codes=sorted(todo),
        origin="auto",
    )
    job_id = next((r["job_id"] for r in results if r.get("job_id")), None)
    if job_id:
        logger.info("gdrive auto: %s/%s → job %s (%d works)", kind, lid, job_id, len(todo))
    return job_id


async def reconcile(only: tuple[str, str] | None = None) -> int:
    """One pass over Drive-enabled listings (or just ``only``). Returns
    how many jobs were created. Serialised so a toggle-triggered run and
    the periodic loop can't both create a job for the same listing."""
    # PikPak in a login cooldown: scans would fail and pile up error
    # jobs (and poke PikPak again). Try again next cycle.
    if pikpak_unavailable() is not None:
        return 0
    async with _lock:
        async with SessionLocal() as session:
            q = select(TrackedListing).where(TrackedListing.gdrive_enabled.is_(True))
            if only is not None:
                q = q.where(TrackedListing.kind == only[0], TrackedListing.id == only[1])
            rows = (await session.execute(q)).scalars().all()
        created = 0
        for row in rows:
            try:
                if await reconcile_listing(row):
                    created += 1
            except Exception as exc:  # noqa: BLE001 — one listing must not stop the pass
                logger.warning("gdrive auto reconcile %s/%s failed: %s", row.kind, row.id, exc)
                await _set_error(row.kind, row.id, f"自動搬移檢查失敗: {exc}")
        return created


def trigger(kind: str, listing_id: str) -> None:
    """Fire-and-forget reconcile for one listing (toggle switched on)."""

    async def _run() -> None:
        try:
            await reconcile((kind, listing_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("gdrive auto trigger %s/%s failed: %s", kind, listing_id, exc)

    asyncio.create_task(_run())


async def run_loop() -> None:
    logger.info("gdrive auto-move loop started")
    await asyncio.sleep(60)  # let the presence index load first
    while True:
        try:
            n = await reconcile()
            if n:
                logger.info("gdrive auto: created %d job(s)", n)
        except Exception as exc:  # noqa: BLE001
            logger.warning("gdrive auto loop hiccup: %s", exc)
        await asyncio.sleep(max(1, settings.gdrive_auto_interval_minutes) * 60)
