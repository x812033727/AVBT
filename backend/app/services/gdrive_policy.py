"""Traffic policy for the PikPak → Drive mover (VPS with metered traffic).

Runtime knobs live in ``app_meta`` so the settings page can change them
without a restart:

- ``gdrive:daily_budget_gb`` — total bytes the backend may stream per
  day across all accounts (0 = unlimited). Every byte is downloaded from
  PikPak AND uploaded to Drive, so VPS traffic ≈ 2 × this.
- ``gdrive:window`` — allowed hours ``HH:MM-HH:MM`` (may wrap midnight;
  empty = all day).
- ``gdrive:paused`` — ``"1"`` stops new chunks (in-flight uploads stop
  at the next chunk boundary and resume later).

Usage is tracked in ``gdrive:usage`` as ``{"day": "YYYY-MM-DD", "bytes": n}``.
"Day" is local to ``settings.gdrive_timezone``. The per-account Google
750 GB/day cap is tracked on ``GDriveAccount`` rows by the queue.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..config import settings
from ..database import SessionLocal
from ..models import AppMeta

logger = logging.getLogger(__name__)

KEY_BUDGET = "gdrive:daily_budget_gb"
KEY_WINDOW = "gdrive:window"
KEY_PAUSED = "gdrive:paused"
KEY_USAGE = "gdrive:usage"

GB = 1024**3


def tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.gdrive_timezone or "UTC")
    except Exception:  # noqa: BLE001 — bad tz name must not kill the queue
        return ZoneInfo("UTC")


def now_local() -> datetime:
    return datetime.now(tz())


def today() -> str:
    return now_local().strftime("%Y-%m-%d")


def parse_window(raw: str) -> tuple[time, time] | None:
    """``"01:00-07:30"`` → (01:00, 07:30). ``None`` for empty/invalid
    (= no restriction). Equal start/end is also treated as all day."""
    raw = (raw or "").strip()
    if not raw or "-" not in raw:
        return None
    a, b = (s.strip() for s in raw.split("-", 1))
    try:
        start = datetime.strptime(a, "%H:%M").time()
        end = datetime.strptime(b, "%H:%M").time()
    except ValueError:
        return None
    if start == end:
        return None
    return start, end


def in_window(window: tuple[time, time] | None, now: time) -> bool:
    if window is None:
        return True
    start, end = window
    if start < end:
        return start <= now < end
    return now >= start or now < end  # wraps midnight


def seconds_until_window(window: tuple[time, time] | None, now: datetime) -> float:
    if window is None or in_window(window, now.time()):
        return 0.0
    start = window[0]
    target = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def seconds_until_tomorrow(now: datetime) -> float:
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return max(1.0, (nxt - now).total_seconds())


@dataclass
class Policy:
    budget_bytes: int  # 0 = unlimited
    window_raw: str
    paused: bool
    used_today: int

    @property
    def window(self) -> tuple[time, time] | None:
        return parse_window(self.window_raw)

    def remaining(self) -> int | None:
        if self.budget_bytes <= 0:
            return None
        return max(0, self.budget_bytes - self.used_today)

    def block_reason(self, now: datetime | None = None) -> str:
        """Empty string when transfers may run right now."""
        now = now or now_local()
        if self.paused:
            return "已暫停"
        if not in_window(self.window, now.time()):
            return f"不在允許時段({self.window_raw})"
        rem = self.remaining()
        if rem is not None and rem <= 0:
            return "今日流量預算已用完"
        return ""

    def to_public(self) -> dict:
        reason = self.block_reason()
        return {
            "daily_budget_gb": round(self.budget_bytes / GB, 2) if self.budget_bytes else 0,
            "window": self.window_raw,
            "paused": self.paused,
            "used_today_bytes": self.used_today,
            "remaining_today_bytes": self.remaining(),
            "blocked_reason": reason,
            "timezone": settings.gdrive_timezone,
        }


async def _get_many(keys: list[str]) -> dict[str, str]:
    async with SessionLocal() as session:
        out: dict[str, str] = {}
        for k in keys:
            row = await session.get(AppMeta, k)
            if row is not None:
                out[k] = row.value or ""
        return out


async def _set(key: str, value: str) -> None:
    async with SessionLocal() as session:
        row = await session.get(AppMeta, key)
        if row is None:
            session.add(AppMeta(key=key, value=value))
        else:
            row.value = value
        await session.commit()


def _parse_usage(raw: str, day: str) -> int:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return 0
    if not isinstance(data, dict) or data.get("day") != day:
        return 0
    try:
        return int(data.get("bytes") or 0)
    except (TypeError, ValueError):
        return 0


async def load() -> Policy:
    vals = await _get_many([KEY_BUDGET, KEY_WINDOW, KEY_PAUSED, KEY_USAGE])
    try:
        budget_gb = float(vals.get(KEY_BUDGET) or 0)
    except ValueError:
        budget_gb = 0.0
    return Policy(
        budget_bytes=int(max(0.0, budget_gb) * GB),
        window_raw=vals.get(KEY_WINDOW, ""),
        paused=vals.get(KEY_PAUSED) == "1",
        used_today=_parse_usage(vals.get(KEY_USAGE, ""), today()),
    )


async def update(
    *, daily_budget_gb: float | None = None, window: str | None = None, paused: bool | None = None
) -> Policy:
    if daily_budget_gb is not None:
        await _set(KEY_BUDGET, str(max(0.0, float(daily_budget_gb))))
    if window is not None:
        w = window.strip()
        if w and parse_window(w) is None:
            raise ValueError("時段格式應為 HH:MM-HH:MM")
        await _set(KEY_WINDOW, w)
    if paused is not None:
        await _set(KEY_PAUSED, "1" if paused else "0")
    return await load()


async def add_usage(nbytes: int) -> None:
    if nbytes <= 0:
        return
    day = today()
    vals = await _get_many([KEY_USAGE])
    used = _parse_usage(vals.get(KEY_USAGE, ""), day) + int(nbytes)
    await _set(KEY_USAGE, json.dumps({"day": day, "bytes": used}))
