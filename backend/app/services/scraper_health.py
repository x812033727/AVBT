"""Scraper health sentinel.

JavBus parsing fails *silently*: a markup change that breaks the
``var gid`` token extraction makes every title look magnet-less, a
Cloudflare challenge page parses as an empty-but-valid listing. Nothing
raises, the tracker just stops finding anything. This module keeps a
small in-memory window of scrape outcomes so that systematic failure
becomes visible (``/api/javbus/scraper-health``, ``scraper_degraded``
on ``/api/health``) and alerts once through the notification queue
instead of never.

Outcome vocabulary — detail pages:

- ``ok_magnets``      tokens extracted, magnet table non-empty
- ``ok_no_magnets``   tokens extracted, AJAX returned nothing (normal
                      for fresh releases — never alerts)
- ``gid_missing``     page parsed (title found) but gid/uc/img missing
                      → the layout-change signal we alert on
- ``empty_parse``     page fetched but neither title nor tokens found
- ``empty_html``      404 / empty body
- ``error``           fetch raised (429 exhausted, 5xx, network, block)

Listing pages: ``ok`` / ``zero_items`` / ``error``. Challenge pages are
context-free and recorded globally via ``record_challenge()``.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, deque

logger = logging.getLogger(__name__)

WINDOW = 200

# Alert rules.
_GID_SAMPLE = 20          # look at the last N detail outcomes...
_GID_MIN_SAMPLE = 10      # ...but stay quiet below this many samples
_GID_RATIO = 0.5          # alert when gid_missing exceeds this ratio
# Error-rate rule: a 429 storm / IP block / persistent age-gate makes
# fetches RAISE (recorded as "error"), unlike a dead code which answers
# empty_html. A high error ratio is the outage signal the breaker reacts
# to but that was invisible on /api/health (2026-07-18 audit). Keyed on
# "error" only, so dead-code empty_html never trips it.
_ERROR_SAMPLE = 20
_ERROR_MIN_SAMPLE = 10
_ERROR_RATIO = 0.5
_CHALLENGE_WINDOW_S = 600.0
_CHALLENGE_COUNT = 3      # alert on >= N challenges inside the window
_ALERT_COOLDOWN_S = 3600.0

_DETAIL_OK = ("ok_magnets", "ok_no_magnets")


class ScraperHealth:
    def __init__(self) -> None:
        # (unix-time, outcome) pairs, newest right.
        self.detail: deque[tuple[float, str]] = deque(maxlen=WINDOW)
        self.listing: deque[tuple[float, str]] = deque(maxlen=WINDOW)
        self.challenges: deque[float] = deque(maxlen=WINDOW)
        self.totals: Counter[str] = Counter()
        self.last_alert_at: dict[str, float] = {}

    # -- recording ---------------------------------------------------

    def record_detail(self, outcome: str) -> None:
        self.detail.append((time.time(), outcome))
        self.totals[f"detail:{outcome}"] += 1
        if outcome == "gid_missing":
            self._check_gid_alert()
        elif outcome == "error":
            self._check_error_alert("detail", self.detail)

    def record_listing(self, outcome: str) -> None:
        self.listing.append((time.time(), outcome))
        self.totals[f"listing:{outcome}"] += 1
        if outcome == "error":
            self._check_error_alert("listing", self.listing)

    # -- alerting ----------------------------------------------------

    def _alert(self, key: str, message: str) -> None:
        now = time.time()
        if now - self.last_alert_at.get(key, 0.0) < _ALERT_COOLDOWN_S:
            return
        self.last_alert_at[key] = now
        logger.error("scraper health alert [%s]: %s", key, message)
        # Local import: notify → config only, so no cycle — but keep it
        # lazy anyway so importing the scraper never drags the queue in.
        from .webhook_queue import webhook_queue

        webhook_queue.enqueue_nowait(f"⚠️ AVBT 爬蟲異常:{message}", event="scraper_alert")

    def record_challenge(self) -> None:
        """Called straight from ``_fetch`` — challenge pages are
        context-free (the same block hits details and listings alike),
        so they're windowed globally rather than per page type."""
        now = time.time()
        self.challenges.append(now)
        recent = sum(1 for t in self.challenges if now - t <= _CHALLENGE_WINDOW_S)
        if recent >= _CHALLENGE_COUNT:
            self._alert(
                "challenge",
                f"{int(_CHALLENGE_WINDOW_S // 60)} 分鐘內遇到 {recent} 次"
                "反機器人挑戰頁,JavBus 可能正在封鎖本機 IP(考慮設 HTTP_PROXY"
                " 或換鏡像站)。",
            )

    def _check_error_alert(self, kind: str, events: deque[tuple[float, str]]) -> None:
        recent = [o for _, o in list(events)[-_ERROR_SAMPLE:]]
        if len(recent) < _ERROR_MIN_SAMPLE:
            return
        errors = sum(1 for o in recent if o == "error")
        if errors / len(recent) > _ERROR_RATIO:
            self._alert(
                f"{kind}_error",
                f"近 {len(recent)} 次{'詳細頁' if kind == 'detail' else '列表頁'}"
                f"有 {errors} 次抓取失敗(429/封鎖/網路)——JavBus 可能正在限流或"
                "封鎖本機 IP(考慮設 HTTP_PROXY 或換鏡像站)。",
            )

    def _check_gid_alert(self) -> None:
        recent = [o for _, o in list(self.detail)[-_GID_SAMPLE:]]
        relevant = [o for o in recent if o in (*_DETAIL_OK, "gid_missing")]
        if len(relevant) < _GID_MIN_SAMPLE:
            return
        missing = sum(1 for o in relevant if o == "gid_missing")
        if missing / len(relevant) > _GID_RATIO:
            self._alert(
                "gid_missing",
                f"近 {len(relevant)} 次詳細頁有 {missing} 次解析到標題卻抽不到"
                "磁力 token——JavBus 頁面結構可能已改版,磁力功能恐已靜默失效。",
            )

    # -- reporting ---------------------------------------------------

    def degraded(self) -> bool:
        """True while any alert condition is inside its cooldown."""
        now = time.time()
        return any(now - t < _ALERT_COOLDOWN_S for t in self.last_alert_at.values())

    def snapshot(self) -> dict:
        now = time.time()

        def window(events: deque[tuple[float, str]]) -> dict:
            counts = Counter(o for _, o in events)
            return {
                "window": len(events),
                "counts": dict(counts),
                "last_event_at": events[-1][0] if events else None,
            }

        return {
            "degraded": self.degraded(),
            "detail": window(self.detail),
            "listing": window(self.listing),
            "challenges_10m": sum(
                1 for t in self.challenges if now - t <= _CHALLENGE_WINDOW_S
            ),
            # Current JavBus rate-limiter spacing vs its floor: when the
            # limiter is penalised by 429s it widens toward the ceiling,
            # so spacing > base is a live throttle signal (2026-07-18).
            "limiter": self._limiter_spacing(),
            "totals": dict(self.totals),
            "last_alert_at": dict(self.last_alert_at),
        }

    @staticmethod
    def _limiter_spacing() -> dict | None:
        """Current/base spacing of the shared JavBus RateLimiter, or None
        if unavailable. Lazy import: the scraper imports this module, so a
        top-level import would be a cycle."""
        try:
            from ..scrapers.javbus import _limiter

            return {
                "current_s": round(_limiter._cur, 3),
                "base_s": round(_limiter._base, 3),
                "penalised": _limiter._cur > _limiter._base,
            }
        except Exception:  # noqa: BLE001 — reporting must never raise
            return None


scraper_health = ScraperHealth()
