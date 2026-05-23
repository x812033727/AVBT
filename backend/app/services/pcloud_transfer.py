"""PikPak → pCloud transfer queue + background worker.

How a transfer plays out:

1. Router enqueues one or more ``PCloudTransfer`` DB rows in
   ``status='pending'`` and signals the worker.
2. Worker picks the oldest pending row(s) (bounded by
   ``settings.pcloud_transfer_concurrency``).
3. For each: get a fresh PikPak download URL → call pCloud
   ``savefilefromurl`` → record ``upload_id`` and flip to ``running``.
4. A second poll loop walks every ``running`` row every
   ``pcloud_poll_interval_seconds`` seconds, asks pCloud for progress,
   and either updates ``bytes_downloaded`` or moves the row to
   ``done`` / ``failed``.
5. When ``done`` and ``delete_source=True``, trash the original PikPak
   file.

Recursive folder transfer (``PCloudTransferRequest.pikpak_folder_id``)
walks the PikPak tree up to a sane depth, mirrors the subfolder layout
under the destination, and enqueues one row per file — all sharing the
same ``parent_id`` so the UI can collapse them.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select, update

from ..config import settings
from ..database import SessionLocal
from ..models import PCloudTransfer
from .pcloud import PCloudError, pcloud_service
from .pikpak import PikPakError, pikpak_service

logger = logging.getLogger(__name__)


# Walk depth cap for recursive PikPak folder transfer. Plenty for typical
# AVBT layouts (root → kind → name → code → files = 5).
_MAX_FOLDER_DEPTH = 8


class PCloudTransferQueue:
    """Long-running background worker. Started in ``main.lifespan``."""

    def __init__(self) -> None:
        self._wakeup = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._inflight: set[int] = set()

    # ---------- lifecycle ----------

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._submit_loop(), name="pcloud-submit")
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop(), name="pcloud-poll")
        # Resurrect any rows the previous process left in "running" — those
        # are recoverable via savefilefromurlstatus(upload_id).
        await self._requeue_stuck()

    async def stop(self) -> None:
        self._stop.set()
        self._wakeup.set()
        for t in (self._task, self._poll_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._task = None
        self._poll_task = None

    def notify(self) -> None:
        self._wakeup.set()

    async def _requeue_stuck(self) -> None:
        """On startup, anything left in 'running' from the previous
        process is either (a) still being downloaded by pCloud — the poll
        loop will pick it up via upload_id — or (b) lost. Don't reset
        them; the poll loop handles both."""
        async with SessionLocal() as session:
            await session.execute(
                update(PCloudTransfer)
                .where(
                    PCloudTransfer.status == "running",
                    PCloudTransfer.pcloud_upload_id == 0,
                )
                .values(status="pending", message="重啟後重新排隊")
            )
            await session.commit()
        self._wakeup.set()

    # ---------- public ----------

    async def status(self) -> dict:
        async with SessionLocal() as session:
            counts = dict(
                (
                    await session.execute(
                        select(PCloudTransfer.status, func.count())
                        .group_by(PCloudTransfer.status)
                    )
                ).all()
            )
        return {
            "pending": int(counts.get("pending", 0)),
            "running": int(counts.get("running", 0)),
            "done": int(counts.get("done", 0)),
            "failed": int(counts.get("failed", 0)),
            "cancelled": int(counts.get("cancelled", 0)),
            "inflight": len(self._inflight),
            "concurrency": settings.pcloud_transfer_concurrency,
        }

    # ---------- enqueue helpers ----------

    async def enqueue_files(
        self,
        files: list[dict],
        *,
        pcloud_folder_id: int,
        pcloud_folder_path: str,
        delete_source: bool,
        parent_id: Optional[int] = None,
    ) -> list[int]:
        """Insert one row per file. ``files`` items look like
        ``{file_id, name, size, source_path}``. Returns the new DB ids."""
        if not files:
            return []
        new_ids: list[int] = []
        async with SessionLocal() as session:
            for f in files:
                row = PCloudTransfer(
                    parent_id=parent_id,
                    pikpak_file_id=f.get("file_id", ""),
                    pikpak_name=f.get("name", ""),
                    pikpak_size=int(f.get("size") or 0),
                    pikpak_path=f.get("source_path", ""),
                    pcloud_folder_id=pcloud_folder_id,
                    pcloud_folder_path=pcloud_folder_path,
                    status="pending",
                    delete_source=delete_source,
                )
                session.add(row)
                await session.flush()
                new_ids.append(row.id)
            await session.commit()
        self.notify()
        return new_ids

    async def walk_pikpak_folder(
        self, folder_id: str, *, cap: int = 5000
    ) -> list[dict]:
        """Recursively list every file under ``folder_id``. Returns rows
        of ``{file_id, name, size, rel_dir}`` where ``rel_dir`` is the
        path under the starting folder (``""`` for direct children).

        Used by the "送整個資料夾" path to mirror PikPak's layout under
        the destination."""
        out: list[dict] = []

        async def _walk(fid: str, depth: int, rel: str) -> None:
            if depth > _MAX_FOLDER_DEPTH or len(out) >= cap:
                return
            try:
                children, _partial = await pikpak_service.list_all_files(fid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("walk_pikpak_folder list failed for %s: %s", fid, exc)
                return
            for c in children:
                if len(out) >= cap:
                    return
                if c.kind == "drive#folder":
                    sub_rel = f"{rel}/{c.name}" if rel else c.name
                    await _walk(c.id, depth + 1, sub_rel)
                else:
                    out.append({
                        "file_id": c.id,
                        "name": c.name,
                        "size": int(c.size or 0),
                        "rel_dir": rel,
                    })

        await _walk(folder_id, 0, "")
        return out

    # ---------- worker loops ----------

    async def _submit_loop(self) -> None:
        """Drains 'pending' rows in batches: bounded by configured
        concurrency. Each iteration grabs (concurrency - inflight) rows,
        sends them to pCloud in parallel, then sleeps on the wakeup
        event until somebody enqueues more or the poll loop frees a slot.
        """
        logger.info("pCloud transfer queue submit loop started")
        while not self._stop.is_set():
            self._wakeup.clear()
            try:
                slots = settings.pcloud_transfer_concurrency - len(self._inflight)
                if slots > 0:
                    rows = await self._claim_pending(slots)
                    if rows:
                        await asyncio.gather(
                            *(self._submit_one(rid) for rid in rows),
                            return_exceptions=True,
                        )
                        continue  # check for more without sleeping
            except Exception as exc:  # noqa: BLE001
                logger.exception("pCloud submit loop hiccup: %s", exc)
                await asyncio.sleep(2.0)
                continue
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                pass

    async def _claim_pending(self, limit: int) -> list[int]:
        """Atomically pick the next ``limit`` pending rows. Sets them to
        'running' so two workers can never grab the same one."""
        if limit <= 0:
            return []
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(PCloudTransfer.id)
                    .where(PCloudTransfer.status == "pending")
                    .order_by(PCloudTransfer.id.asc())
                    .limit(limit)
                )
            ).scalars().all()
            if not rows:
                return []
            await session.execute(
                update(PCloudTransfer)
                .where(PCloudTransfer.id.in_(rows))
                .values(status="running", message="準備傳輸…")
            )
            await session.commit()
        for rid in rows:
            self._inflight.add(rid)
        return list(rows)

    async def _submit_one(self, transfer_id: int) -> None:
        """Drive a single transfer up to the point where pCloud has
        accepted it (got an upload_id). After that, the poll loop owns
        the row until terminal state."""
        try:
            async with SessionLocal() as session:
                row = await session.get(PCloudTransfer, transfer_id)
                if row is None or row.status != "running":
                    return
                file_id = row.pikpak_file_id
                folder_id = row.pcloud_folder_id
                name = row.pikpak_name

            try:
                links = await pikpak_service.file_links(file_id)
            except PikPakError as exc:
                await self._mark(transfer_id, "failed", f"PikPak 連結取得失敗: {exc}")
                return
            except Exception as exc:  # noqa: BLE001
                await self._mark(transfer_id, "failed", f"PikPak 錯誤: {exc}")
                return

            url = (links or {}).get("download_url") or ""
            if not url:
                await self._mark(transfer_id, "failed", "PikPak 沒有可用下載連結")
                return

            try:
                resp = await pcloud_service.save_file_from_url(
                    url, folder_id, filename=name
                )
            except PCloudError as exc:
                await self._mark(transfer_id, "failed", f"pCloud 拒絕: {exc}")
                return
            except Exception as exc:  # noqa: BLE001
                await self._mark(transfer_id, "failed", f"pCloud 錯誤: {exc}")
                return

            upload_id = int(resp.get("upload_id") or 0)
            async with SessionLocal() as session:
                await session.execute(
                    update(PCloudTransfer)
                    .where(PCloudTransfer.id == transfer_id)
                    .values(
                        pcloud_upload_id=upload_id,
                        message="pCloud 已接受,下載中…" if upload_id else "pCloud 回應無 upload_id",
                        status="running" if upload_id else "failed",
                    )
                )
                await session.commit()
        finally:
            self._inflight.discard(transfer_id)
            # Free a slot → wake submit_loop in case more pendings exist.
            self._wakeup.set()

    async def _poll_loop(self) -> None:
        """Every N seconds, ask pCloud about every 'running' row that has
        an upload_id. Translates the response into 'done' / 'failed' /
        progress updates."""
        logger.info("pCloud transfer queue poll loop started")
        interval = max(5, int(settings.pcloud_poll_interval_seconds or 15))
        while not self._stop.is_set():
            try:
                await asyncio.sleep(interval)
                await self._poll_running()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("pCloud poll loop hiccup: %s", exc)

    async def _poll_running(self) -> None:
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(
                        PCloudTransfer.id,
                        PCloudTransfer.pcloud_upload_id,
                        PCloudTransfer.delete_source,
                        PCloudTransfer.pikpak_file_id,
                    )
                    .where(
                        PCloudTransfer.status == "running",
                        PCloudTransfer.pcloud_upload_id != 0,
                    )
                )
            ).all()
        if not rows:
            return
        for rid, upload_id, delete_source, pikpak_fid in rows:
            try:
                p = await pcloud_service.upload_progress(int(upload_id))
            except Exception as exc:  # noqa: BLE001
                logger.warning("pCloud progress poll failed for %s: %s", rid, exc)
                continue
            status = p.get("status")
            if status == "downloading":
                async with SessionLocal() as session:
                    await session.execute(
                        update(PCloudTransfer)
                        .where(PCloudTransfer.id == rid)
                        .values(
                            bytes_downloaded=int(p.get("downloaded") or 0),
                            message=f"pCloud 下載中 ({p.get('downloaded',0)}/{p.get('size',0)})",
                        )
                    )
                    await session.commit()
            elif status == "done":
                async with SessionLocal() as session:
                    await session.execute(
                        update(PCloudTransfer)
                        .where(PCloudTransfer.id == rid)
                        .values(
                            pcloud_file_id=int(p.get("file_id") or 0),
                            status="done",
                            message="完成",
                            finished_at=datetime.utcnow(),
                            bytes_downloaded=int(
                                (p.get("metadata") or {}).get("size") or 0
                            ),
                        )
                    )
                    await session.commit()
                if delete_source and pikpak_fid:
                    try:
                        await pikpak_service.trash_files([pikpak_fid])
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "post-transfer PikPak trash failed for %s: %s",
                            pikpak_fid, exc,
                        )
            elif status == "failed":
                await self._mark(
                    rid, "failed",
                    f"pCloud 下載失敗: {p.get('error') or 'unknown'}",
                )
            elif status == "unknown":
                # pCloud has no record — assume lost; mark failed so the
                # user can retry.
                await self._mark(
                    rid, "failed", "pCloud 找不到此上傳任務(可能已逾時或被取消)",
                )

    async def _mark(self, transfer_id: int, status: str, message: str) -> None:
        async with SessionLocal() as session:
            values: dict = {"status": status, "message": message}
            if status in ("done", "failed", "cancelled"):
                values["finished_at"] = datetime.utcnow()
            await session.execute(
                update(PCloudTransfer)
                .where(PCloudTransfer.id == transfer_id)
                .values(**values)
            )
            await session.commit()

    # ---------- caller actions ----------

    async def retry(self, transfer_id: int) -> bool:
        async with SessionLocal() as session:
            row = await session.get(PCloudTransfer, transfer_id)
            if row is None:
                return False
            if row.status not in ("failed", "cancelled"):
                return False
            row.status = "pending"
            row.pcloud_upload_id = 0
            row.bytes_downloaded = 0
            row.message = "已重新排隊"
            row.finished_at = None
            await session.commit()
        self.notify()
        return True

    async def cancel(self, transfer_id: int) -> bool:
        async with SessionLocal() as session:
            row = await session.get(PCloudTransfer, transfer_id)
            if row is None:
                return False
            if row.status not in ("pending", "running"):
                return False
            upload_id = row.pcloud_upload_id
            row.status = "cancelled"
            row.message = "已取消"
            row.finished_at = datetime.utcnow()
            await session.commit()
        if upload_id:
            try:
                await pcloud_service.cancel_upload(int(upload_id))
            except Exception as exc:  # noqa: BLE001
                logger.warning("pCloud cancel upload %s failed: %s", upload_id, exc)
        self._inflight.discard(transfer_id)
        self.notify()
        return True

    async def cleanup(self, *, keep_failed: bool = True) -> int:
        """Drop rows in terminal states (done / cancelled, optionally
        failed). Returns count deleted."""
        async with SessionLocal() as session:
            states = ["done", "cancelled"]
            if not keep_failed:
                states.append("failed")
            res = await session.execute(
                select(PCloudTransfer.id).where(PCloudTransfer.status.in_(states))
            )
            ids = res.scalars().all()
            if ids:
                from sqlalchemy import delete
                await session.execute(
                    delete(PCloudTransfer).where(PCloudTransfer.id.in_(ids))
                )
                await session.commit()
            return len(ids)


pcloud_transfer_queue = PCloudTransferQueue()
