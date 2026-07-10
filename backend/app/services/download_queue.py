"""Global download queue.

A single ``DownloadQueue`` singleton coordinates every PikPak offline
submission in the system. Three call sites enqueue into it:

- The tracker (``services/tracker.py``) for periodic auto-send.
- Bulk send-all streams (``services/bulk.py``) for "send this listing".
- Single submits and bulk submits (``routers/pikpak.py``).

The queue has a fixed worker pool (``settings.download_queue_concurrency``,
default 5). Workers each pull one job at a time, fetch the JavBus detail
when needed, pick the best magnet, submit to PikPak, and record an
``OfflineTaskLog`` row. Each enqueue returns an ``asyncio.Future`` that
resolves to a ``JobResult`` when the work is done, so callers that want
to stream progress (the bulk-send NDJSON endpoints) can ``as_completed``
over their futures.

The queue also:

- **Coalesces duplicates** in-flight: enqueueing the same ``code`` twice
  while the first is still pending/processing returns the *same* future,
  so both callers see the same outcome and PikPak only sees one submit.
- **Skips DB-duplicates** automatically: if the chosen magnet's btih is
  already in ``offline_task_log``, the job resolves as
  ``skipped_already_sent`` without hitting PikPak (unless ``force=True``).
- **Supports cancellation** by ``batch_id``: bulk send-all assigns each
  batch a UUID so cancelling the HTTP request drops the still-pending
  jobs (in-flight ones run to completion).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Literal

from sqlalchemy import insert, select

from ..config import settings
from ..database import SessionLocal
from ..models import OfflineTaskLog
from ..schemas import OfflineSubmit, SendAllOptions
from ..scrapers import javbus as scraper
from ..scrapers.javbus import extract_btih, pick_best_magnet
from .pikpak import pikpak_service
from .supervisor import supervise
from .webhook_queue import webhook_queue

logger = logging.getLogger(__name__)


JobStatus = Literal[
    "sent",
    "skipped_no_magnet",
    "skipped_already_sent",
    "failed",
    "cancelled",
]


@dataclass
class JobResult:
    code: str
    status: JobStatus
    message: str = ""
    magnet_name: str = ""
    magnet_size: str = ""
    is_hd: bool = False
    has_subtitle: bool = False
    task_id: str = ""

    def to_event(self) -> dict:
        """Shape used by the NDJSON ``progress`` event."""
        return {
            "code": self.code,
            "status": self.status,
            "message": self.message,
            "magnet_name": self.magnet_name,
            "size": self.magnet_size,
            "is_hd": self.is_hd,
            "has_subtitle": self.has_subtitle,
        }


@dataclass
class Job:
    code: str  # uppercased canonical JAV code; "" only for direct-magnet jobs
    options: SendAllOptions
    source: str  # free-form label, e.g. "tracker:star:abc" / "bulk:genre:foo"
    batch_id: str = ""
    # Direct submit: skip the JavBus detail fetch + magnet pick step.
    # Used by /api/pikpak/offline where the caller already has a magnet.
    direct_magnet: str = ""
    force: bool = False
    folder: str | None = None
    # Snapshot of the tracked listing context at enqueue time. When all
    # three are populated, the archiver can route this code to the
    # right kind/name folder without a JavBus fetch_detail call.
    tracked_kind: str = ""
    tracked_slug: str = ""
    tracked_name: str = ""
    on_sent: Callable[[str], Awaitable[None]] | None = None
    enqueued_at: datetime = field(default_factory=datetime.utcnow)
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())

    def dedup_key(self) -> str:
        """The key used to coalesce duplicate enqueues. For code-jobs we
        use the code; for direct-magnet jobs we use the magnet's btih (or
        the full magnet string when no btih) so two clicks of the same
        magnet collapse instead of double-submitting."""
        if self.direct_magnet:
            h = extract_btih(self.direct_magnet)
            return f"magnet:{h or self.direct_magnet}"
        return f"code:{self.code.upper()}"


class DownloadQueue:
    """Single-process, bounded-concurrency download dispatcher."""

    def __init__(self, concurrency: int = 5) -> None:
        self._concurrency = max(1, concurrency)
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        # dedup_key → Job, for in-flight + pending coalescing
        self._pending: dict[str, Job] = {}
        self._processing: dict[str, Job] = {}
        self._lock = asyncio.Lock()
        self._workers: list[asyncio.Task] = []
        self._started = False
        # Lifetime stats
        self._totals: dict[str, int] = {
            "sent": 0,
            "skipped_no_magnet": 0,
            "skipped_already_sent": 0,
            "failed": 0,
            "cancelled": 0,
        }
        # Most-recent N results (newest first)
        self._recent: deque[tuple[datetime, Job, JobResult]] = deque(maxlen=50)

    # ---------- lifecycle ----------

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for i in range(self._concurrency):
            self._workers.append(
                supervise(partial(self._worker, i), f"download-worker-{i}")
            )
        logger.info("download queue started with %d workers", self._concurrency)

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        for w in self._workers:
            try:
                await w
            except asyncio.CancelledError:
                pass
        self._workers.clear()
        self._started = False

    # ---------- public API ----------

    async def enqueue(self, job: Job) -> asyncio.Future:
        """Enqueue ``job`` and return a future resolving to its ``JobResult``.

        If an identical job (same code, or same magnet hash for direct
        submits) is already pending or processing AND the caller didn't
        set ``force``, the existing job's future is returned and the new
        job is discarded — so both callers observe the same outcome and
        PikPak only sees one submission."""
        key = job.dedup_key()
        async with self._lock:
            if not job.force:
                existing = self._pending.get(key) or self._processing.get(key)
                if existing is not None:
                    return existing.future
            self._pending[key] = job
        await self._queue.put(job)
        return job.future

    async def cancel_batch(self, batch_id: str) -> int:
        """Drop pending jobs tagged with ``batch_id``, resolve their
        futures as ``cancelled``. In-flight jobs run to completion.

        Returns the number of jobs removed."""
        if not batch_id:
            return 0
        cancelled = 0
        async with self._lock:
            # asyncio.Queue has no random-access remove, so drain + repop.
            held: list[Job] = []
            while True:
                try:
                    held.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            for j in held:
                if j.batch_id == batch_id:
                    self._pending.pop(j.dedup_key(), None)
                    if not j.future.done():
                        j.future.set_result(
                            JobResult(code=j.code, status="cancelled")
                        )
                    self._record(j, JobResult(code=j.code, status="cancelled"))
                    cancelled += 1
                else:
                    self._queue.put_nowait(j)
                    # Don't call task_done() — that's done by the worker
                    # when it actually finishes the item.
            # The drained items we re-put need their queue.task_done()
            # accounting balanced. asyncio.Queue tracks "unfinished tasks"
            # via put/task_done pairs, not via get/put. Since each
            # cancelled job was put() but never task_done()'d, we must
            # task_done() now so the accounting stays consistent.
            for _ in range(cancelled):
                self._queue.task_done()
        return cancelled

    def status(self) -> dict:
        """Snapshot of the queue state — safe to call from request
        handlers without awaiting (just reads cached dicts/counters)."""
        return {
            "concurrency": self._concurrency,
            "pending": len(self._pending),
            "processing": [
                {"code": j.code or j.dedup_key(), "source": j.source}
                for j in list(self._processing.values())
            ],
            "totals": dict(self._totals),
            "recent": [
                {
                    "at": at.isoformat(),
                    "code": j.code or j.dedup_key(),
                    "source": j.source,
                    "status": r.status,
                    "message": r.message,
                    "magnet_name": r.magnet_name,
                }
                for (at, j, r) in list(self._recent)
            ],
        }

    # ---------- worker loop ----------

    async def _worker(self, idx: int) -> None:
        while True:
            job = await self._queue.get()
            key = job.dedup_key()
            async with self._lock:
                self._pending.pop(key, None)
                self._processing[key] = job

            try:
                if job.future.cancelled():
                    # Caller went away before we picked it up. Don't
                    # bother submitting.
                    result = JobResult(code=job.code, status="cancelled")
                else:
                    result = await self._process(job)
            except asyncio.CancelledError:
                # Worker itself cancelled (shutdown). Leave the future
                # unresolved — the calling stream will time out / error.
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("download queue worker %d crashed on %s", idx, job.code)
                result = JobResult(
                    code=job.code, status="failed", message=str(exc)
                )
            finally:
                # Resolve the future BEFORE dropping the in-flight entry,
                # so a concurrent re-enqueue (same code, no force) sees
                # "still in flight" and coalesces onto the just-resolved
                # future instead of starting a second PikPak submission.
                if not job.future.done():
                    job.future.set_result(result)
                async with self._lock:
                    self._processing.pop(key, None)
                self._record(job, result)
                self._queue.task_done()

    def _record(self, job: Job, result: JobResult) -> None:
        self._totals[result.status] = self._totals.get(result.status, 0) + 1
        self._recent.appendleft((datetime.utcnow(), job, result))
        # One hook covers every failure path (fetch, submit, worker
        # crash). Event is OFF by default — see notify_download_failed.
        if result.status == "failed":
            webhook_queue.enqueue_nowait(
                f"❌ 下載送出失敗 `{job.code or result.magnet_name or '?'}`: {result.message}",
                event="download_failed",
            )

    # ---------- per-job processing ----------

    async def _process(self, job: Job) -> JobResult:
        """Run one job end-to-end: pick magnet (if needed), submit to
        PikPak, log to DB, fire on_sent. Returns a ``JobResult``."""
        # ---- direct-magnet branch (single POST /api/pikpak/offline) ----
        if job.direct_magnet:
            return await self._process_direct(job)

        # ---- code branch (tracker / bulk / wishlist) ----
        try:
            detail = await scraper.fetch_detail_resolved(job.code)
        except Exception as exc:  # noqa: BLE001
            return JobResult(
                code=job.code,
                status="failed",
                message=f"抓取詳細頁失敗: {exc}",
            )

        if not detail.magnets:
            return JobResult(code=job.code, status="skipped_no_magnet")

        opts = job.options
        sent_hashes = await _load_sent_hashes() if opts.skip_sent else set()
        best = pick_best_magnet(
            detail.magnets,
            hd_only=opts.hd_only,
            subtitle_only=opts.subtitle_only,
            skip_hashes=sent_hashes if opts.skip_sent else set(),
            min_size_mb=opts.min_size_mb,
            max_size_mb=opts.max_size_mb,
            prefer_max_size_mb=opts.prefer_max_size_mb,
        )
        if best is None:
            any_already = opts.skip_sent and any(
                extract_btih(m.link) in sent_hashes for m in detail.magnets
            )
            if any_already:
                return JobResult(code=job.code, status="skipped_already_sent")
            return JobResult(code=job.code, status="skipped_no_magnet")

        try:
            task = await pikpak_service.offline_download(
                OfflineSubmit(magnet=best.link, code=job.code, folder=opts.folder)
            )
        except Exception as exc:  # noqa: BLE001
            return JobResult(
                code=job.code,
                status="failed",
                message=f"送 PikPak 失敗: {exc}",
            )

        await _log_offline_task(
            code=job.code,
            magnet=best.link,
            task_id=task.id,
            file_id=task.file_id or "",
            name=task.name,
            phase=task.phase,
            message=task.message or "",
            tracked_kind=job.tracked_kind,
            tracked_slug=job.tracked_slug,
            tracked_name=job.tracked_name,
        )

        if job.on_sent is not None:
            try:
                await job.on_sent(job.code)
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_sent hook for %s failed: %s", job.code, exc)

        return JobResult(
            code=job.code,
            status="sent",
            magnet_name=best.name,
            magnet_size=best.size,
            is_hd=best.is_hd,
            has_subtitle=best.has_subtitle,
            task_id=task.id,
        )

    async def _process_direct(self, job: Job) -> JobResult:
        h = extract_btih(job.direct_magnet)
        if not job.force and h:
            # Same btih cache the code-job path uses — saves a DB
            # round-trip per direct submit (the cache is authoritative:
            # warmed at startup, appended on every logged send).
            if h in await _load_sent_hashes():
                return JobResult(
                    code=job.code,
                    status="skipped_already_sent",
                    message="此磁力已經送過 PikPak（force=true 可強制再送）",
                )

        try:
            task = await pikpak_service.offline_download(
                OfflineSubmit(
                    magnet=job.direct_magnet,
                    code=job.code,
                    folder=job.folder,
                    force=job.force,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return JobResult(
                code=job.code,
                status="failed",
                message=f"送 PikPak 失敗: {exc}",
            )

        await _log_offline_task(
            code=job.code,
            magnet=job.direct_magnet,
            task_id=task.id,
            file_id=task.file_id or "",
            name=task.name,
            phase=task.phase,
            message=task.message or "",
            tracked_kind=job.tracked_kind,
            tracked_slug=job.tracked_slug,
            tracked_name=job.tracked_name,
        )

        if job.on_sent is not None:
            try:
                await job.on_sent(job.code)
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_sent hook for %s failed: %s", job.code, exc)

        return JobResult(
            code=job.code,
            status="sent",
            magnet_name=task.name,
            task_id=task.id,
        )


# ---------- shared helpers (also used by routers/services that still
# need direct DB access) ----------


# Process-wide cache of OfflineTaskLog.btih values. The download queue
# uses this on every job (when ``opts.skip_sent`` is True) to filter out
# magnets already sent to PikPak. The set is lazily loaded the first
# time anyone asks for it, and appended-to on every successful
# ``_log_offline_task`` — so a bulk send of 500 codes only touches the
# DB once (on the first lookup) instead of running a full table scan
# per job. The backend is the sole writer of ``OfflineTaskLog`` so the
# append-on-success hook is sufficient; nothing else needs invalidation.
_sent_hashes_cache: set[str] | None = None
_sent_hashes_lock = asyncio.Lock()


async def _load_sent_hashes() -> set[str]:
    global _sent_hashes_cache
    if _sent_hashes_cache is not None:
        return _sent_hashes_cache
    async with _sent_hashes_lock:
        if _sent_hashes_cache is not None:
            return _sent_hashes_cache
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(OfflineTaskLog.btih).where(OfflineTaskLog.btih != "")
                )
            ).scalars().all()
        _sent_hashes_cache = set(rows)
        logger.info("sent-hash cache loaded: %d entries", len(_sent_hashes_cache))
    return _sent_hashes_cache


async def warm_sent_hashes() -> None:
    """Pre-load the sent-hash cache during lifespan startup so the
    first job pulled by a worker doesn't pay the full-table-scan
    latency."""
    await _load_sent_hashes()


async def all_sent_hashes() -> set[str]:
    """Read-only view of the btih cache for routers. Callers must not
    mutate the returned set."""
    return await _load_sent_hashes()


def _note_sent_hash(magnet: str) -> None:
    """Append a newly-submitted btih to the cache, if it's already
    populated. If the cache hasn't loaded yet (no skip_sent job has
    arrived), do nothing — the eventual lazy load will pick it up."""
    if _sent_hashes_cache is None:
        return
    h = extract_btih(magnet)
    if h:
        _sent_hashes_cache.add(h)


async def _log_offline_task(
    *,
    code: str,
    magnet: str,
    task_id: str,
    file_id: str,
    name: str,
    phase: str,
    message: str,
    tracked_kind: str = "",
    tracked_slug: str = "",
    tracked_name: str = "",
) -> None:
    async with SessionLocal() as session:
        await session.execute(
            insert(OfflineTaskLog).values(
                code=code,
                magnet=magnet,
                btih=extract_btih(magnet),
                task_id=task_id,
                file_id=file_id,
                name=name,
                phase=phase,
                message=message,
                tracked_kind=tracked_kind,
                tracked_slug=tracked_slug,
                tracked_name=tracked_name,
            )
        )
        await session.commit()
    _note_sent_hash(magnet)


def new_batch_id() -> str:
    return uuid.uuid4().hex


# Singleton. ``settings`` is already loaded by the time this module is
# imported (config.py has no cyclic dependency on us), so the configured
# concurrency takes effect immediately — no late re-binding needed.
download_queue = DownloadQueue(concurrency=settings.download_queue_concurrency)
