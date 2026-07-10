"""In-memory job manager for long-running pCloud operations.

Decouples the work (a possibly slow ``organize_folder_stream`` pass over
many JavBus lookups and pCloud moves) from the HTTP request lifetime.
The client kicks the job off with ``POST /files/organize/jobs``, then
polls ``GET /files/organize/jobs/{id}`` for progress. Closing the
browser only cancels the polling — the background ``asyncio.Task`` keeps
running.

Trade-offs:

- **In-memory only**. Jobs disappear on process restart. We don't try
  to checkpoint to disk: a half-finished organize already mutated
  pCloud, so on next boot we have nothing useful to resume from
  anyway. The user just looks at pCloud and reruns if needed.
- **Bounded job history**. Each job is a small dict plus its event
  list (≤ N events, where N == folder size; ~50 KB for a 100-file
  folder). Finished jobs are pruned oldest-first past
  ``_KEEP_FINISHED`` so a long-lived process can't accumulate
  unbounded memory; running jobs are never pruned.
- **One organize per folder at a time**. ``create_organize_job``
  rejects with 409 if there's already a running job for the same
  folder_id, to avoid two passes racing each other on the same set of
  pCloud children.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


_STATUS_RUNNING = "running"
_STATUS_DONE = "done"
_STATUS_ERROR = "error"
_STATUS_CANCELLED = "cancelled"


@dataclass
class OrganizeJob:
    job_id: str
    folder_id: str
    folder_name: str
    dry_run: bool
    status: str  # running | done | error | cancelled
    started_at: str  # ISO 8601 UTC
    finished_at: str | None = None
    total: int = 0
    events: list[dict] = field(default_factory=list)
    # The most recent "processing" heartbeat — `None` between items
    # (i.e. when the previous child's progress event has been emitted
    # and the next child hasn't started its first await).
    processing: dict | None = None
    result: dict | None = None
    error: str | None = None
    # Internal: the asyncio.Task driving the work. Excluded from
    # serialisation — clients only ever see the public fields.
    task: asyncio.Task | None = field(default=None, repr=False)

    def to_public_dict(self, *, since: int = 0) -> dict[str, Any]:
        """Serialise to a JSON-safe shape for HTTP responses.

        ``since`` lets the client fetch only the new tail of events —
        polling stays O(delta) instead of O(total). The response always
        includes ``next_since`` so the client knows what to send next.
        """
        return {
            "job_id": self.job_id,
            "folder_id": self.folder_id,
            "folder_name": self.folder_name,
            "dry_run": self.dry_run,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total": self.total,
            "processing": self.processing,
            "events": self.events[since:] if since > 0 else self.events,
            "next_since": len(self.events),
            "result": self.result,
            "error": self.error,
        }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


_KEEP_FINISHED = 50


class OrganizeJobManager:
    def __init__(self, keep_finished: int = _KEEP_FINISHED) -> None:
        self._jobs: dict[str, OrganizeJob] = {}
        self._keep_finished = max(1, keep_finished)

    def create(
        self, folder_id: str, folder_name: str, dry_run: bool
    ) -> OrganizeJob:
        # 12 hex chars = 48 bits of entropy. Enough to be unique within
        # a process lifetime even with thousands of jobs.
        job_id = uuid4().hex[:12]
        job = OrganizeJob(
            job_id=job_id,
            folder_id=folder_id,
            folder_name=folder_name,
            dry_run=dry_run,
            status=_STATUS_RUNNING,
            started_at=_now_iso(),
        )
        self._jobs[job_id] = job
        self._prune()
        return job

    def _prune(self) -> None:
        """Drop the oldest finished jobs past the retention cap. A
        client polling a just-pruned id gets 404 — acceptable, since
        pruning only happens once 50+ newer jobs exist."""
        finished = [
            j for j in self._jobs.values() if j.status != _STATUS_RUNNING
        ]
        excess = len(finished) - self._keep_finished
        if excess <= 0:
            return
        finished.sort(key=lambda j: j.finished_at or j.started_at)
        for job in finished[:excess]:
            self._jobs.pop(job.job_id, None)

    def get(self, job_id: str) -> OrganizeJob | None:
        return self._jobs.get(job_id)

    def active_for_folder(self, folder_id: str) -> OrganizeJob | None:
        for job in self._jobs.values():
            if job.status == _STATUS_RUNNING and job.folder_id == folder_id:
                return job
        return None

    def list_jobs(
        self,
        *,
        folder_id: str | None = None,
        status: str | None = None,
    ) -> list[OrganizeJob]:
        jobs = list(self._jobs.values())
        if folder_id:
            jobs = [j for j in jobs if j.folder_id == folder_id]
        if status:
            jobs = [j for j in jobs if j.status == status]
        # Newest first — most useful default for a "what's been running
        # recently" list.
        jobs.sort(key=lambda j: j.started_at, reverse=True)
        return jobs

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.task is None:
            return False
        if job.status != _STATUS_RUNNING:
            return False
        job.task.cancel()
        return True

    async def run(self, job: OrganizeJob, service) -> None:
        """Drive ``service.organize_folder_stream`` and project its
        events into the job's mutable state. Runs as a background task —
        the caller spawns it with ``asyncio.create_task`` and stores the
        task on ``job.task`` so cancellation works.
        """
        try:
            async for event in service.organize_folder_stream(
                job.folder_id, dry_run=job.dry_run
            ):
                etype = event.get("type")
                if etype == "start":
                    job.total = int(event.get("total") or 0)
                elif etype == "processing":
                    job.processing = {
                        "current": event.get("current"),
                        "source": event.get("source"),
                        "kind": event.get("kind"),
                    }
                elif etype == "progress":
                    job.events.append(event)
                    job.processing = None
                elif etype == "done":
                    job.result = event.get("result")
                elif etype == "error":
                    job.error = str(event.get("message") or "未知錯誤")
                    job.status = _STATUS_ERROR
                    job.finished_at = _now_iso()
                    return
            job.status = _STATUS_DONE
            job.finished_at = _now_iso()
        except asyncio.CancelledError:
            job.status = _STATUS_CANCELLED
            job.finished_at = _now_iso()
            job.processing = None
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("organize job %s crashed", job.job_id)
            job.error = str(exc)
            job.status = _STATUS_ERROR
            job.finished_at = _now_iso()
            job.processing = None


organize_job_manager = OrganizeJobManager()
