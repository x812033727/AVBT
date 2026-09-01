"""Spread PikPak's monthly offline-download traffic across the month.

PikPak meters cloud-download traffic separately from storage. The
traffic meter (``transfer_quota()["transfer"]["base"]["offline"]``) is
40 TiB and resets to zero on the 1st, Taipei time; the storage meter
(``quota()``) can read perfectly healthy while traffic is exhausted and
every offline submit comes back ``Cloud Download Traffic 40.x T has
exceeded the limit 40 T`` (HTTP 502). Confusing the two costs a day of
misdiagnosis — see ``PikPakService.transfer_quota``'s docstring.

Observed twice running: the whole month's traffic is consumed in the
month's first third and the pipeline then sits dark. The 2026-08-01
03:15 CST reset was spent by 08-06 07:40 — 5.2 days, ~44 TB, 6,722
tasks at ~6.5 GB each. Sustainable is 43.98 TB / 31 days = 1.42 TB/day,
roughly 218 tasks; 08-03 alone submitted 2,745.

The gate is a feedback loop, not a forecast: a magnet's size is unknown
before submitting, so instead of predicting cost we watch the live meter
and stop once today has consumed its share of what is left. Each day's
share is ``(limit - used_at_day_start) / days_remaining_in_month``, so
an under-spent day widens tomorrow's share and an over-spent one narrows
it, with no separate carry-over ledger to drift out of sync.
"""

from __future__ import annotations

import calendar
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from ..database import SessionLocal
from ..models import AppMeta
from .pikpak import pikpak_service

logger = logging.getLogger(__name__)

# PikPak's traffic month rolls at midnight Taipei time, so the day and
# month boundaries this gate slices on must use the same clock — UTC
# would shift the reset eight hours into the previous day.
TAIPEI = timezone(timedelta(hours=8))

# app_meta keys. Runtime-adjustable state belongs here per CLAUDE.md;
# the baseline in particular must survive a restart, which the rota
# performs several times a day.
_KEY_ENABLED = "traffic:daily_gate_enabled"
_KEY_DAY = "traffic:day_stamp"
_KEY_BASELINE = "traffic:day_start_used"

# The meter only moves as bytes land, and a job takes minutes — reading
# it per job would add a PikPak round trip to every submit for a number
# that cannot have meaningfully changed.
_CACHE_TTL = 60.0
_cache: tuple[float, int, int] | None = None  # (monotonic, used, limit)


@dataclass(frozen=True)
class TrafficPlan:
    """Today's verdict. ``allowance``/``spent_today`` are bytes."""

    is_open: bool
    allowance: int
    spent_today: int
    used: int
    limit: int
    rebaselined: bool = False
    degraded: bool = False

    def reason(self) -> str:
        if self.degraded:
            return "traffic quota unreadable — gate open (PikPak still enforces)"
        if self.is_open:
            return (
                f"today {self.spent_today / 1e9:.1f} GB of "
                f"{self.allowance / 1e9:.1f} GB"
            )
        return (
            f"daily traffic share spent: {self.spent_today / 1e9:.1f} GB of "
            f"{self.allowance / 1e9:.1f} GB "
            f"(month {self.used / 1e12:.2f}/{self.limit / 1e12:.2f} TB)"
        )


def _days_left_in_month(today: date) -> int:
    """Including today, so the last day of the month still gets a share
    rather than dividing by zero."""
    return calendar.monthrange(today.year, today.month)[1] - today.day + 1


def plan(*, used: int, limit: int, day_start_used: int,
         today: date) -> TrafficPlan:
    """Pure decision: does another submit fit inside today's share?"""
    if limit <= 0:
        # Junk read. Fail OPEN: PikPak hard-rejects at 100% on its own,
        # so it stays the real backstop, and failing closed here would
        # turn a transient API blip into a full pipeline outage.
        return TrafficPlan(is_open=True, allowance=0, spent_today=0,
                           used=used, limit=limit, degraded=True)

    # A drop means the month rolled and PikPak zeroed the meter. The
    # reset lands at ~00:00 Taipei on the 1st — the same moment the day
    # stamp rolls — so a baseline captured just before it would be far
    # above the new reading, driving spent_today negative and the
    # allowance off a baseline above the limit. That would hold the gate
    # shut for all of day 1 of a brand-new budget.
    rebaselined = used < day_start_used
    if rebaselined:
        day_start_used = used

    remaining = max(0, limit - day_start_used)
    allowance = remaining // _days_left_in_month(today)
    spent_today = max(0, used - day_start_used)
    return TrafficPlan(
        is_open=spent_today < allowance,
        allowance=allowance,
        spent_today=spent_today,
        used=used,
        limit=limit,
        rebaselined=rebaselined,
    )


async def _read_meter(force: bool = False) -> tuple[int, int]:
    """(used, limit) for ``base.offline``. (0, 0) when unreadable."""
    global _cache
    if not force and _cache is not None and time.monotonic() - _cache[0] < _CACHE_TTL:
        return _cache[1], _cache[2]
    try:
        raw = await pikpak_service.transfer_quota()
        offline = (
            raw.get("transfer", {}).get("base", {}).get("offline", {})
            if isinstance(raw, dict) else {}
        )
        used = int(offline.get("assets") or 0)
        limit = int(offline.get("total_assets") or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("traffic meter unreadable: %s", exc)
        return 0, 0
    _cache = (time.monotonic(), used, limit)
    return used, limit


async def _meta(session, key: str) -> str | None:
    row = await session.get(AppMeta, key)
    return row.value if row is not None else None


async def _set_meta(session, key: str, value: str) -> None:
    row = await session.get(AppMeta, key)
    if row is None:
        row = AppMeta(key=key)
        session.add(row)
    row.value = value


async def _baseline(used: int, today: date) -> int:
    """Today's starting meter reading, persisted so a restart mid-day
    doesn't hand the pipeline a fresh full share (the rota redeploys
    several times a day)."""
    async with SessionLocal() as session:
        stamp = await _meta(session, _KEY_DAY)
        stored = await _meta(session, _KEY_BASELINE)
        if stamp == today.isoformat() and stored is not None:
            try:
                baseline = int(stored)
            except ValueError:
                baseline = used
            if used >= baseline:
                return baseline
            # Meter went backwards → the month reset. Fall through so the
            # new baseline is persisted rather than recomputed every call.
        await _set_meta(session, _KEY_DAY, today.isoformat())
        await _set_meta(session, _KEY_BASELINE, str(used))
        await session.commit()
    return used


async def enabled() -> bool:
    """Default on. Setting ``traffic:daily_gate_enabled`` to "0" in
    app_meta is the kill switch — no redeploy needed."""
    async with SessionLocal() as session:
        raw = await _meta(session, _KEY_ENABLED)
    return raw != "0"


async def current_plan() -> TrafficPlan:
    used, limit = await _read_meter()
    today = datetime.now(TAIPEI).date()
    baseline = await _baseline(used, today)
    return plan(used=used, limit=limit, day_start_used=baseline, today=today)


async def gate_open() -> tuple[bool, str]:
    """(may we submit, why not). Never raises — a failure here must not
    be able to stop downloads."""
    try:
        if not await enabled():
            return True, "gate disabled"
        p = await current_plan()
        return p.is_open, p.reason()
    except Exception as exc:  # noqa: BLE001
        logger.warning("traffic gate check failed, allowing submit: %s", exc)
        return True, "gate check failed — allowing"
