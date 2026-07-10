import json
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import TrackedListing
from ..schemas import (
    AggregatedMissing,
    CheckListingResult,
    MissingCodesResult,
    MissingSummary,
    TrackedListingIn,
    TrackedListingOut,
)
from ..scrapers import javbus as scraper
from ..services import missing as missing_svc
from ..services import tracker

router = APIRouter(prefix="/api/tracked", tags=["tracked"])

# Subset of LISTING_KINDS that we can sensibly track for "new works".
# Genre changes too fast to be useful as a "new" feed.
_ALLOWED = {"star", "studio", "label", "series", "director"}


def _to_out(r: TrackedListing) -> TrackedListingOut:
    return TrackedListingOut(
        kind=r.kind,
        id=r.id,
        name=r.name,
        avatar=r.avatar,
        uncensored=bool(r.uncensored),
        auto_send=bool(r.auto_send),
        last_seen_code=r.last_seen_code,
        last_checked_at=r.last_checked_at,
        last_error=r.last_error,
        new_count=int(r.new_count or 0),
        created_at=r.created_at,
    )


# ---------- tracker-loop status ----------


@router.get("/status")
async def tracker_status():
    return tracker.state.to_dict()


@router.post("/status/toggle")
async def tracker_toggle(enabled: bool = Body(..., embed=True)):
    tracker.state.enabled = enabled
    return tracker.state.to_dict()


@router.post("/status/toggle-backfill")
async def tracker_toggle_backfill(enabled: bool = Body(..., embed=True)):
    tracker.state.backfill_enabled = enabled
    return tracker.state.to_dict()


@router.post("/status/run-now/stream")
async def tracker_run_now_stream():
    """Streaming variant of ``/status/run-now`` — yields NDJSON events
    so the UI can show "X / Y" progress as each listing completes
    instead of staring at a spinner for the whole batch."""
    missing_svc.invalidate_all_caches(presence=True)

    async def gen():
        new_total = 0
        try:
            async for event in tracker.check_all_stream(force=True):
                if event.get("type") == "progress":
                    new_total += len(event.get("new_codes") or [])
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"
        finally:
            tracker.state.last_new_total = new_total
            tracker.state.last_run = datetime.utcnow()
            # The batch shifted last_seen / queued downloads — drop the
            # result cache so the post-batch reload from the UI sees
            # fresh aggregate data.
            missing_svc.invalidate_all_caches()

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.post("/status/run-now")
async def tracker_run_now():
    # Batch check explicitly requested by the user — drop any cached
    # PikPak inventory so auto-send-missing and the post-batch missing-
    # summary re-fetch see the current state of the cloud. ``force=True``
    # bypasses the "complete listing skipped for 24h" rule so the user's
    # manual request actually re-checks every listing.
    missing_svc.invalidate_all_caches(presence=True)
    results = await tracker.check_all(force=True)
    new_total = sum(len(r.get("new_codes") or []) for r in results)
    tracker.state.last_new_total = new_total
    tracker.state.last_run = datetime.utcnow()
    # The batch may have queued downloads / shifted last_seen — drop
    # the result cache one more time so the post-batch reload from
    # the UI sees fresh data.
    missing_svc.invalidate_all_caches()
    return {
        "results": results,
        "new_total": new_total,
        **tracker.state.to_dict(),
    }


# ---------- missing-codes ----------

# Registered ABOVE the catch-all /{kind}/{slug:path} so the literal
# "missing-summary" / "missing-all" paths win over the slug route.


@router.get("/missing-summary", response_model=MissingSummary)
async def missing_summary_endpoint(refresh: bool = False):
    return await missing_svc.missing_summary(refresh=refresh)


@router.post("/missing-summary/stream")
async def missing_summary_stream_endpoint(refresh: bool = True):
    """Streaming variant of ``/missing-summary`` — yields NDJSON events
    as each listing's JavBus catalog walk completes. Lets the "重算缺漏"
    button show "X / Y" progress instead of a blank spinner for minutes.

    POST (not GET) for parity with the other ``/stream`` endpoints and so
    the frontend's shared ``streamNdjson`` helper can hit it without a
    method-specific branch."""
    async def gen():
        try:
            async for event in missing_svc.missing_summary_stream(refresh=refresh):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.get("/missing-all", response_model=AggregatedMissing)
async def missing_all_endpoint(refresh: bool = False):
    return await missing_svc.missing_all(refresh=refresh)


# ---------- CRUD ----------


@router.get("", response_model=list[TrackedListingOut])
async def list_tracked(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(
            select(TrackedListing).order_by(TrackedListing.created_at.desc())
        )
    ).scalars().all()
    return [_to_out(r) for r in rows]


@router.post("", response_model=TrackedListingOut)
async def upsert_tracked(
    payload: TrackedListingIn, session: AsyncSession = Depends(get_session)
):
    kind = payload.kind.strip()
    slug = payload.id.strip()
    # Be forgiving when callers paste a URL fragment like "series/11pb"
    # or a fully-qualified "/series/11pb/" — trim the kind prefix and
    # any surrounding slashes so we always store a clean slug.
    slug = slug.strip("/")
    if slug.lower().startswith(f"{kind.lower()}/"):
        slug = slug[len(kind) + 1:]
    slug = slug.strip("/")
    if "/" in slug:
        raise HTTPException(
            status_code=400,
            detail=f"slug 不可含斜線（你給的：{payload.id!r}）",
        )
    if kind not in _ALLOWED:
        raise HTTPException(status_code=400, detail=f"不支援的 kind: {kind}")
    if not slug:
        raise HTTPException(status_code=400, detail="missing id")

    row = await session.get(TrackedListing, (kind, slug))
    auto_send_just_enabled = False
    if row:
        auto_send_just_enabled = (
            bool(payload.auto_send) and not bool(row.auto_send)
        )
        row.name = payload.name or row.name
        row.avatar = payload.avatar or row.avatar
        row.uncensored = payload.uncensored
        row.auto_send = payload.auto_send
    else:
        # When the caller didn't supply a display name, try to extract
        # one from the listing's page header. Falls back to the slug.
        resolved_name = payload.name.strip()
        if not resolved_name:
            try:
                resolved_name = await scraper.fetch_listing_title(
                    kind, slug, uncensored=payload.uncensored
                )
            except Exception:  # noqa: BLE001 — title fetch is best-effort
                resolved_name = ""
            if not resolved_name:
                resolved_name = slug
        row = TrackedListing(
            kind=kind,
            id=slug,
            name=resolved_name,
            avatar=payload.avatar,
            uncensored=payload.uncensored,
            auto_send=payload.auto_send,
            created_at=datetime.utcnow(),
        )
        session.add(row)
        auto_send_just_enabled = bool(payload.auto_send)
    await session.commit()
    await session.refresh(row)

    # When the user newly turns auto_send ON, kick off a missing-codes
    # backfill right away instead of making them wait for the next
    # hourly tracker cycle. The enqueue helper combines fresh codes (we
    # pass [] here — nothing new since baseline) with the catalog's
    # missing-from-PikPak set, and pushes both into the global queue.
    if auto_send_just_enabled:
        import asyncio

        from ..services.tracker import _enqueue_auto_send  # local: avoid cycles
        asyncio.create_task(_enqueue_auto_send(kind, slug, []))

    # The tracked-listing set changed (or its display name did); drop
    # the cached aggregate so the next /missing-summary rebuilds.
    missing_svc.invalidate_all_caches()
    return _to_out(row)


# slug:path so legacy / mis-entered slugs that contain a slash (e.g.
# "series/11pb") can still be looked up and deleted. New writes go
# through upsert_tracked which strips the prefix.
#
# Note: the GET /{kind}/{slug:path} catch-all is greedy — any literal
# suffix endpoints (/missing-codes, /check, /reset-new-count) MUST be
# declared *before* it, otherwise FastAPI routes "star/abc/missing-codes"
# to get_tracked with slug="abc/missing-codes" and the suffix is lost.


@router.get("/{kind}/{slug:path}/missing-codes", response_model=MissingCodesResult)
async def missing_codes_for(
    kind: str,
    slug: str,
    refresh: bool = False,
    uncensored: bool = False,
    dedup: bool = True,
    session: AsyncSession = Depends(get_session),
):
    # Prefer the DB row's uncensored flag when present — keeps client
    # callers concise (just /missing-codes, no extra query string).
    #
    # dedup=true (default) hides codes claimed by an earlier-ordered
    # listing under the global first-seen rule, matching the badge count
    # from /missing-summary. Pass ?dedup=false to see this listing's
    # full missing catalog regardless of overlap.
    row = await session.get(TrackedListing, (kind, slug))
    eff_uncensored = bool(row.uncensored) if row else uncensored
    return await missing_svc.missing_for_listing(
        kind, slug, uncensored=eff_uncensored, refresh=refresh, dedup=dedup
    )


@router.get("/{kind}/{slug:path}", response_model=TrackedListingOut)
async def get_tracked(
    kind: str, slug: str, session: AsyncSession = Depends(get_session)
):
    row = await session.get(TrackedListing, (kind, slug))
    if not row:
        raise HTTPException(status_code=404, detail="not tracked")
    return _to_out(row)


@router.delete("/{kind}/{slug:path}")
async def untrack(
    kind: str, slug: str, session: AsyncSession = Depends(get_session)
):
    row = await session.get(TrackedListing, (kind, slug))
    if not row:
        raise HTTPException(status_code=404, detail="not tracked")
    await session.delete(row)
    await session.commit()
    missing_svc.invalidate_all_caches()
    return {"ok": True}


@router.post("/{kind}/{slug:path}/check/stream")
async def check_now_stream(kind: str, slug: str):
    """Streaming variant of ``/check`` — yields ``start`` / ``progress``
    / ``done`` events around the page-1 and missing-scan phases so the
    UI button can show "page 1…" / "掃描缺漏…" instead of a silent spinner
    while the catalog walk runs."""
    missing_svc.invalidate_all_caches(presence=True)

    async def gen():
        try:
            async for event in tracker.check_listing_stream(kind, slug, force=True):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.post("/{kind}/{slug:path}/check", response_model=CheckListingResult)
async def check_now(kind: str, slug: str):
    # User explicitly asked for a fresh check. Drop any cached PikPak
    # presence so the auto_send-missing path (and the missing-codes
    # badge re-fetch the UI does afterwards) sees the current state
    # of the cloud, not a stale snapshot from before the user deleted
    # files / moved things around. ``force=True`` so a manual check
    # always walks the JavBus catalog (refreshing the missing-count) and
    # is never silently skipped by the daily-cadence rule for complete
    # listings.
    missing_svc.invalidate_all_caches(presence=True)
    return CheckListingResult(
        **await tracker.check_listing(kind, slug, force=True)
    )


@router.post("/{kind}/{slug:path}/reset-new-count")
async def reset_new_count(
    kind: str, slug: str, session: AsyncSession = Depends(get_session)
):
    row = await session.get(TrackedListing, (kind, slug))
    if not row:
        raise HTTPException(status_code=404, detail="not tracked")
    row.new_count = 0
    await session.commit()
    return {"ok": True}
