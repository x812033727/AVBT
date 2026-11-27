"""PikPak → Google Drive mover: per-studio jobs + streaming upload worker.

Flow:

1. ``create_jobs`` inserts one ``GDriveJob`` per selected studio and
   scans it in the background: every downloaded code of the studio
   (``studio_index``) → its PikPak leaf paths (``presence_index``) → the
   files under each leaf. One ``GDriveTransfer`` row per file; the Drive
   folder is the PikPak parent path mirrored verbatim (``drive_path_for``),
   so ``AVBT/製作商/<studio>/<series>/<code>/`` looks the same on both.
2. The worker claims pending rows (``gdrive_transfer_concurrency``) and
   streams each one: PikPak download URL (HTTP Range from the committed
   offset) → Drive resumable session, chunk by chunk. Nothing touches the
   disk. Before every chunk the traffic policy (pause / time window /
   daily budget) and the account's daily cap are checked; when blocked
   the row is parked back to ``pending`` with its session URI and offset
   kept, and resumes later from the same byte.
3. Finished + size-verified files get a ``GDriveLocation`` row and (for
   videos) an anyone-with-link permission for the embedded player.
4. When every file of a job is done, the PikPak source leaves are
   trashed (if ``delete_source``) and the indexes refreshed. Any failure
   leaves the job ``partial`` and the source untouched; the already-moved
   originals of a partial job are only trashed on explicit request
   (``trash_source``), never automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
from datetime import datetime, timedelta

import httpx
from sqlalchemy import delete, func, or_, select, update

from ..config import settings
from ..database import SessionLocal
from ..models import GDriveAccount, GDriveJob, GDriveLocation, GDriveTransfer
from . import gdrive_accounts, gdrive_policy, listing_codes, studio_index
from .gdrive import (
    AccountAuth,
    GDriveAuthError,
    GDriveError,
    GDriveQuotaError,
    SessionExpired,
    chunk_size,
    drive_path_for,
    gdrive_client,
)
from .jav_code import is_video, normalize_code
from .pikpak import PikPakError, PikPakLoginBlocked, pikpak_service
from .pikpak_presence import presence_index
from .supervisor import supervise
from .webhook_queue import webhook_queue

logger = logging.getLogger(__name__)

_MAX_LEAF_DEPTH = 4
ACTIVE_JOB_STATES = ("scanning", "transferring")
_TERMINAL = ("done", "failed", "cancelled")


# PikPakError messages meaning "PikPak can't be used right now" without a
# typed exception (no credentials configured). Login cooldowns raise
# PikPakLoginBlocked instead. Transfers wait instead of burning retries.
_PIKPAK_UNAVAILABLE_MARKERS = ("尚未登入",)


def pikpak_unavailable() -> tuple[str, float] | None:
    """``(reason, seconds_left)`` while PikPak is in a login cooldown."""
    try:
        st = pikpak_service.status()
    except Exception:  # noqa: BLE001 — status must never break the worker
        return None
    secs = float(st.get("login_cooldown_seconds") or 0)
    if secs <= 0:
        return None
    minutes = max(1, int(secs // 60) + (1 if secs % 60 else 0))
    why = st.get("login_block_reason") or ""
    reason = f"PikPak 登入冷卻中(約剩 {minutes} 分鐘)" + (f":{why}" if why else "")
    return reason, secs


class Paused(Exception):  # noqa: N818 — control flow, not an error
    """Upload stopped at a chunk boundary; row goes back to pending."""

    def __init__(self, reason: str, wait_seconds: float) -> None:
        super().__init__(reason)
        self.reason = reason
        self.wait_seconds = wait_seconds


class Cancelled(Exception):  # noqa: N818
    pass


# ---------------------------------------------------------------------------
# pure helpers (unit-tested)


def plan_leaf_files(leaf_path: str, leaf_is_folder: bool, files: list[dict]) -> list[dict]:
    """Map the files found under one PikPak code leaf to transfer specs.

    ``files`` items: ``{file_id, name, size, rel_dir}`` (``rel_dir`` is
    relative to the leaf folder; ignored for a bare-file leaf). Returns
    ``{file_id, name, size, pikpak_path, drive_dir, drive_code_dir}``.
    Zero-byte files are dropped (nothing worth moving, and Drive's
    resumable protocol needs special-casing for them)."""
    leaf_path = leaf_path.strip("/")
    if leaf_is_folder:
        code_dir = leaf_path
    else:
        code_dir = posixpath.dirname(leaf_path)
    out: list[dict] = []
    for f in files:
        size = int(f.get("size") or 0)
        if size <= 0:
            continue
        if leaf_is_folder:
            rel = (f.get("rel_dir") or "").strip("/")
            parent = f"{leaf_path}/{rel}" if rel else leaf_path
        else:
            parent = code_dir
        out.append({
            "file_id": f["file_id"],
            "name": f["name"],
            "size": size,
            "pikpak_path": f"{parent}/{f['name']}",
            "drive_dir": drive_path_for(parent),
            "drive_code_dir": drive_path_for(code_dir),
        })
    return out


def job_outcome(counts: dict[str, int]) -> str | None:
    """Terminal job state from per-status file counts, or ``None`` while
    work remains. ``done`` only when every file is done."""
    if counts.get("pending", 0) or counts.get("running", 0):
        return None
    if counts.get("failed", 0) or counts.get("cancelled", 0):
        return "partial"
    return "done"


def retry_delay(attempts: int, base: int) -> int:
    return max(1, base) * (2 ** max(0, attempts - 1))


# ---------------------------------------------------------------------------


class GDriveTransferQueue:
    def __init__(self) -> None:
        self._wakeup = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._inflight: set[int] = set()
        self._scans: dict[int, asyncio.Task] = {}
        self._job_locks: dict[int, asyncio.Lock] = {}
        self._scan_lock = asyncio.Lock()

    # ---------- lifecycle ----------

    async def start(self) -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(GDriveTransfer)
                .where(GDriveTransfer.status == "running")
                .values(status="pending", message="重啟後重新排隊(會從已上傳位置續傳)")
            )
            await session.execute(
                update(GDriveJob)
                .where(GDriveJob.status == "scanning")
                .values(status="error", message="掃描被重啟中斷,請重新搬移", finished_at=datetime.utcnow())
            )
            await session.commit()
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = supervise(
                self._loop, "gdrive-transfer", should_restart=lambda: not self._stop.is_set()
            )

    async def stop(self) -> None:
        self._stop.set()
        self._wakeup.set()
        tasks = [t for t in (self._task, *self._scans.values()) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        self._scans.clear()
        await gdrive_client.aclose()

    def notify(self) -> None:
        self._wakeup.set()

    # ---------- jobs ----------

    async def create_jobs(
        self,
        targets: list[tuple[str, str, str]],
        *,
        account_id: int,
        delete_source: bool,
        codes: list[str] | None = None,
        origin: str = "manual",
    ) -> list[dict]:
        """One job per listing ``(kind, id, name)``. Listings that already
        have an active job are skipped. ``codes`` limits the job to those
        works (auto jobs); ``None`` = every downloaded work of the listing."""
        await gdrive_accounts.credentials(account_id)  # fail fast
        results: list[dict] = []
        async with SessionLocal() as session:
            active = set(
                (
                    await session.execute(
                        select(GDriveJob.listing_kind, GDriveJob.studio_id).where(
                            GDriveJob.status.in_(ACTIVE_JOB_STATES)
                        )
                    )
                ).all()
            )
            new_jobs: list[GDriveJob] = []
            seen: set[tuple[str, str]] = set()
            for kind, lid, name in targets:
                lid = (lid or "").strip()
                if not lid or (kind, lid) in seen:
                    continue
                seen.add((kind, lid))
                if (kind, lid) in active:
                    results.append(
                        {"kind": kind, "studio_id": lid, "job_id": None, "error": "已有進行中的搬移"}
                    )
                    continue
                job = GDriveJob(
                    listing_kind=kind,
                    studio_id=lid,
                    studio_name=name or lid,
                    account_id=account_id,
                    status="scanning",
                    delete_source=delete_source,
                    source_state="pending" if delete_source else "keep",
                    codes=json.dumps(sorted(set(codes))) if codes else "",
                    origin=origin,
                    message="掃描 PikPak 檔案中…",
                )
                session.add(job)
                new_jobs.append(job)
            await session.commit()
        for job in new_jobs:
            results.append(
                {"kind": job.listing_kind, "studio_id": job.studio_id, "job_id": job.id, "error": ""}
            )
            self._scans[job.id] = asyncio.create_task(self._scan_job(job.id))
        return results

    async def _scan_job(self, job_id: int) -> None:
        try:
            await self._scan_job_inner(job_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — record, don't crash the app
            logger.exception("gdrive scan job %s failed", job_id)
            await self._set_job(job_id, status="error", message=f"掃描失敗: {exc}", finished=True)
        finally:
            self._scans.pop(job_id, None)

    async def _scan_job_inner(self, job_id: int) -> None:
        async with SessionLocal() as session:
            job = await session.get(GDriveJob, job_id)
            if job is None:
                return
            kind, listing_id, account_id = job.listing_kind, job.studio_id, job.account_id
            wanted = job.codes
        if wanted:
            codes = sorted(json.loads(wanted))
        else:
            codes = sorted(await listing_codes.codes_for_listing(kind, listing_id))
        if not codes:
            await self._set_job(job_id, status="error", message="沒有已下載的作品", finished=True)
            return

        specs: list[dict] = []
        missing: list[str] = []
        for code in codes:
            paths = presence_index.paths_for(code)
            if not paths:
                missing.append(code)
                continue
            for leaf in paths:
                for spec in await self._files_for_leaf(leaf):
                    spec["code"] = normalize_code(code) or code
                    specs.append(spec)

        # Check-and-insert under one lock so two overlapping listings'
        # scans can't both queue the same file.
        async with self._scan_lock:
            # Never move a file twice: skip what is already on Drive (any
            # account — a work can belong to several Drive-enabled listings)
            # or queued in another active job.
            async with SessionLocal() as session:
                done_paths = set(
                    (await session.execute(select(GDriveLocation.path))).scalars().all()
                )
                busy_ids = set(
                    (
                        await session.execute(
                            select(GDriveTransfer.pikpak_file_id).where(
                                GDriveTransfer.job_id != job_id,
                                GDriveTransfer.status.in_(("pending", "running")),
                            )
                        )
                    ).scalars().all()
                )

            skipped = 0
            rows: list[GDriveTransfer] = []
            for spec in specs:
                if drive_path_for(spec["pikpak_path"]) in done_paths or spec["file_id"] in busy_ids:
                    skipped += 1
                    continue
                rows.append(
                    GDriveTransfer(
                        job_id=job_id,
                        account_id=account_id,
                        code=spec["code"],
                        pikpak_file_id=spec["file_id"],
                        pikpak_name=spec["name"],
                        pikpak_size=spec["size"],
                        pikpak_path=spec["pikpak_path"],
                        source_leaf_id=spec["leaf_id"],
                        drive_dir=spec["drive_dir"],
                        drive_code_dir=spec["drive_code_dir"],
                        status="pending",
                        message="排隊中",
                    )
                )
            async with SessionLocal() as session:
                session.add_all(rows)
                job = await session.get(GDriveJob, job_id)
                if job is None or job.status != "scanning":
                    await session.rollback()
                    return
                job.code_count = len(codes) - len(missing)
                job.file_count = len(rows)
                job.total_bytes = sum(r.pikpak_size for r in rows)
                notes = []
                if skipped:
                    notes.append(f"{skipped} 個檔案已在 Drive 或其他搬移中,略過")
                if missing:
                    notes.append(f"{len(missing)} 部在 PikPak 找不到路徑")
                job.message = ";".join(notes)
                job.status = "transferring" if rows else "done"
                if not rows:
                    job.finished_at = datetime.utcnow()
                    job.source_state = "keep"
                    job.message = job.message or "沒有需要搬移的檔案"
                await session.commit()
        self.notify()

    async def _files_for_leaf(self, leaf_path: str) -> list[dict]:
        """Resolve a presence path (code folder or bare video file) and
        list every file under it, tagged with the leaf's PikPak id."""
        parent, name = posixpath.split(leaf_path.strip("/"))
        parent_id = await pikpak_service.lookup_folder_id(parent, strict=True)
        if not parent_id:
            return []
        children, _partial = await pikpak_service.list_all_files(parent_id)
        leaf = next((c for c in children if c.name == name), None)
        if leaf is None:
            return []
        if leaf.kind == "drive#folder":
            found: list[dict] = []
            await self._walk(leaf.id, "", 0, found)
            specs = plan_leaf_files(leaf_path, True, found)
        else:
            specs = plan_leaf_files(
                leaf_path, False,
                [{"file_id": leaf.id, "name": leaf.name, "size": leaf.size or 0}],
            )
        for s in specs:
            s["leaf_id"] = leaf.id
        return specs

    async def _walk(self, folder_id: str, rel: str, depth: int, out: list[dict]) -> None:
        children, _partial = await pikpak_service.list_all_files(folder_id)
        for c in children:
            if c.kind == "drive#folder":
                if depth < _MAX_LEAF_DEPTH:
                    await self._walk(c.id, f"{rel}/{c.name}" if rel else c.name, depth + 1, out)
            else:
                out.append({"file_id": c.id, "name": c.name, "size": c.size or 0, "rel_dir": rel})

    async def _set_job(self, job_id: int, *, finished: bool = False, **values) -> None:
        if finished:
            values["finished_at"] = datetime.utcnow()
        async with SessionLocal() as session:
            await session.execute(update(GDriveJob).where(GDriveJob.id == job_id).values(**values))
            await session.commit()

    # ---------- worker ----------

    async def _loop(self) -> None:
        logger.info("gdrive transfer worker started")
        while not self._stop.is_set():
            self._wakeup.clear()
            wait = 30.0
            try:
                blocked = pikpak_unavailable()
                if blocked is not None:
                    # Every claimed row would just fail on file_links (and
                    # poke PikPak again, lengthening the cooldown) — hold
                    # the whole queue until the cooldown ends.
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(), timeout=min(600.0, blocked[1] + 5)
                        )
                    except TimeoutError:
                        pass
                    continue
                policy = await gdrive_policy.load()
                now = gdrive_policy.now_local()
                reason = policy.block_reason(now)
                if reason:
                    if policy.paused:
                        wait = 300.0
                    elif not gdrive_policy.in_window(policy.window, now.time()):
                        wait = min(600.0, gdrive_policy.seconds_until_window(policy.window, now))
                    else:
                        wait = min(600.0, gdrive_policy.seconds_until_tomorrow(now))
                else:
                    slots = max(1, settings.gdrive_transfer_concurrency) - len(self._inflight)
                    if slots > 0:
                        ids = await self._claim(slots)
                        for rid in ids:
                            asyncio.create_task(self._run_one(rid))
                        if ids:
                            continue
            except Exception as exc:  # noqa: BLE001
                logger.exception("gdrive worker hiccup: %s", exc)
                wait = 10.0
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=max(1.0, wait))
            except TimeoutError:
                pass

    async def _claim(self, limit: int) -> list[int]:
        day = gdrive_policy.today()
        async with SessionLocal() as session:
            candidates = (
                await session.execute(
                    select(GDriveTransfer.id, GDriveTransfer.account_id)
                    .where(
                        GDriveTransfer.status == "pending",
                        or_(
                            GDriveTransfer.next_retry_at.is_(None),
                            GDriveTransfer.next_retry_at <= datetime.utcnow(),
                        ),
                    )
                    .order_by(GDriveTransfer.id.asc())
                    .limit(limit * 20)
                )
            ).all()
            if not candidates:
                return []
            acc_ids = {a for _, a in candidates}
            accounts = {
                a.id: a
                for a in (
                    await session.execute(select(GDriveAccount).where(GDriveAccount.id.in_(acc_ids)))
                ).scalars()
            }
            picked: list[int] = []
            capped: list[int] = []
            for rid, aid in candidates:
                acc = accounts.get(aid)
                if acc is not None and gdrive_accounts.remaining_today(acc, day) <= 0:
                    capped.append(rid)
                    continue
                if len(picked) < limit:
                    picked.append(rid)
            if capped:
                tomorrow = datetime.utcnow() + timedelta(
                    seconds=gdrive_policy.seconds_until_tomorrow(gdrive_policy.now_local())
                )
                await session.execute(
                    update(GDriveTransfer)
                    .where(GDriveTransfer.id.in_(capped))
                    .values(next_retry_at=tomorrow, message="帳號今日上傳額度(750 GB)已滿,明天繼續")
                )
            if picked:
                await session.execute(
                    update(GDriveTransfer)
                    .where(GDriveTransfer.id.in_(picked), GDriveTransfer.status == "pending")
                    .values(status="running", message="準備上傳…")
                )
            await session.commit()
        self._inflight.update(picked)
        return picked

    async def _run_one(self, rid: int) -> None:
        job_id = 0
        try:
            async with SessionLocal() as session:
                row = await session.get(GDriveTransfer, rid)
                if row is None or row.status != "running":
                    return
                job_id = row.job_id
            await self._upload(rid)
        except Paused as p:
            await self._park(rid, p.reason, p.wait_seconds)
        except Cancelled:
            pass
        except GDriveQuotaError as exc:
            if exc.reason == "storageQuotaExceeded":
                await self._fail(rid, f"Drive 空間已滿: {exc}")
            else:
                async with SessionLocal() as session:
                    row = await session.get(GDriveTransfer, rid)
                    aid = row.account_id if row else 0
                if aid:
                    await gdrive_accounts.mark_daily_cap_hit(aid)
                await self._park(
                    rid,
                    "Google 回報今日上傳額度已滿,明天繼續",
                    gdrive_policy.seconds_until_tomorrow(gdrive_policy.now_local()),
                )
        except PikPakLoginBlocked as exc:
            # An account-wide outage, not this file's fault: every claim
            # fails instantly, and the retry ladder (120+240+480+960s) is
            # exactly one 30-min cooldown — counting it would fail the
            # whole queue before PikPak comes back. Park past the cooldown.
            await self._park(
                rid, "PikPak 登入冷卻中,冷卻結束後自動繼續", exc.remaining + 30.0
            )
        except PikPakError as exc:
            if any(m in str(exc) for m in _PIKPAK_UNAVAILABLE_MARKERS):
                await self._park(rid, "PikPak 尚未登入,登入後自動繼續", 600.0)
            else:
                logger.warning("gdrive transfer %s PikPak error: %s", rid, exc)
                await self._fail_or_retry(rid, f"PikPak 錯誤: {exc}")
        except (GDriveAuthError, gdrive_accounts.AccountUnavailable) as exc:
            async with SessionLocal() as session:
                row = await session.get(GDriveTransfer, rid)
                aid = row.account_id if row else 0
            if aid:
                await gdrive_accounts.set_error(aid, str(exc))
            await self._fail(rid, f"帳號無法使用: {exc}(重新授權後按重試)")
        except Exception as exc:  # noqa: BLE001
            logger.warning("gdrive transfer %s failed: %s", rid, exc)
            await self._fail_or_retry(rid, str(exc) or exc.__class__.__name__)
        finally:
            self._inflight.discard(rid)
            self._wakeup.set()
            if job_id:
                await self._maybe_finish_job(job_id)

    async def _check_continue(self, rid: int, account_id: int) -> None:
        """Between chunks: honour cancel, pause/window/budget, account cap."""
        async with SessionLocal() as session:
            status = (
                await session.execute(select(GDriveTransfer.status).where(GDriveTransfer.id == rid))
            ).scalar_one_or_none()
            acc = await session.get(GDriveAccount, account_id)
        if status != "running":
            raise Cancelled()
        policy = await gdrive_policy.load()
        now = gdrive_policy.now_local()
        reason = policy.block_reason(now)
        if reason:
            if policy.paused:
                wait = 60.0
            elif not gdrive_policy.in_window(policy.window, now.time()):
                wait = gdrive_policy.seconds_until_window(policy.window, now)
            else:
                wait = gdrive_policy.seconds_until_tomorrow(now)
            raise Paused(reason, wait)
        if acc is not None and gdrive_accounts.remaining_today(acc) <= 0:
            raise Paused(
                "帳號今日上傳額度(750 GB)已滿,明天繼續",
                gdrive_policy.seconds_until_tomorrow(now),
            )

    async def _upload(self, rid: int) -> None:
        async with SessionLocal() as session:
            row = await session.get(GDriveTransfer, rid)
            assert row is not None
            account_id = row.account_id
            size = row.pikpak_size
            name = row.pikpak_name
            pikpak_fid = row.pikpak_file_id
            drive_dir = row.drive_dir
            session_uri = row.session_uri
        auth = await gdrive_accounts.credentials(account_id)
        await self._check_continue(rid, account_id)

        folder_id = await gdrive_client.ensure_path(auth, drive_dir)
        offset = 0
        result: dict | None = None
        if session_uri:
            try:
                offset, result = await gdrive_client.query_upload(session_uri, size)
            except SessionExpired:
                session_uri, offset = "", 0
        if not session_uri:
            session_uri = await gdrive_client.start_upload(
                auth, name=name, folder_id=folder_id, size=size
            )
            offset = 0
        await self._save_progress(rid, session_uri=session_uri, offset=offset, folder_id=folder_id)

        if result is None:
            result = await self._stream(rid, account_id, pikpak_fid, session_uri, offset, size)
        await self._complete(rid, result, auth)

    async def _stream(
        self, rid: int, account_id: int, pikpak_fid: str, session_uri: str, offset: int, size: int
    ) -> dict:
        links = await pikpak_service.file_links(pikpak_fid)
        url = (links or {}).get("download_url") or ""
        if not url:
            raise GDriveError("PikPak 沒有可用下載連結")
        csize = chunk_size()
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        async with gdrive_client.http().stream(
            "GET", url, headers=headers, timeout=httpx.Timeout(120.0, connect=30.0)
        ) as resp:
            if resp.status_code >= 400:
                raise GDriveError(f"PikPak 下載失敗 HTTP {resp.status_code}")
            # A server that ignores Range sends from byte 0: drop the prefix.
            skip = offset if (offset and resp.status_code != 206) else 0
            buf = bytearray()
            async for piece in resp.aiter_bytes(1024 * 1024):
                if skip:
                    if len(piece) <= skip:
                        skip -= len(piece)
                        continue
                    piece = piece[skip:]
                    skip = 0
                buf += piece
                while len(buf) >= csize:
                    offset, result = await self._send(
                        rid, account_id, session_uri, bytes(buf[:csize]), offset, size
                    )
                    del buf[:csize]
                    if result is not None:
                        return result
            # Final (short) chunk — only when it really ends the file.
            if buf and offset + len(buf) == size:
                offset, result = await self._send(
                    rid, account_id, session_uri, bytes(buf), offset, size
                )
                if result is not None:
                    return result
        # Stream ended early or Drive wants more — ask where it stands.
        committed, result = await gdrive_client.query_upload(session_uri, size)
        if result is not None:
            return result
        await self._save_progress(rid, offset=committed)
        raise GDriveError(f"PikPak 串流提前結束({committed}/{size}),稍後續傳")

    async def _send(
        self, rid: int, account_id: int, session_uri: str, data: bytes, offset: int, size: int
    ) -> tuple[int, dict | None]:
        committed, result = await gdrive_client.put_chunk(session_uri, data, offset, size)
        delta = max(0, committed - offset)
        await gdrive_policy.add_usage(delta)
        await gdrive_accounts.add_uploaded(account_id, delta)
        await self._save_progress(rid, offset=committed)
        if result is None:
            if committed != offset + len(data):
                # Partial commit: resend from Drive's committed offset on
                # the next attempt (rare; aligned chunks normally land whole).
                raise GDriveError(f"Drive 只收到部分區塊({committed}/{offset + len(data)}),稍後續傳")
            await self._check_continue(rid, account_id)
        return committed, result

    async def _save_progress(
        self, rid: int, *, offset: int, session_uri: str | None = None, folder_id: str | None = None
    ) -> None:
        values: dict = {"bytes_uploaded": offset}
        if session_uri is not None:
            values["session_uri"] = session_uri
        if folder_id is not None:
            values["drive_folder_id"] = folder_id
        async with SessionLocal() as session:
            row = await session.get(GDriveTransfer, rid)
            if row is not None and row.pikpak_size:
                pct = offset * 100 // row.pikpak_size
                values["message"] = f"上傳中 {pct}%"
            await session.execute(update(GDriveTransfer).where(GDriveTransfer.id == rid).values(**values))
            await session.commit()

    async def _complete(self, rid: int, result: dict, auth: AccountAuth) -> None:
        async with SessionLocal() as session:
            row = await session.get(GDriveTransfer, rid)
            assert row is not None
            account_id, size = row.account_id, row.pikpak_size
            job_id, code, name = row.job_id, row.code, row.pikpak_name
            code_dir, path = row.drive_code_dir, row.pikpak_path
            folder_id = row.drive_folder_id
        file_id = result.get("id") or ""
        got = int(result.get("size") or 0)
        if not file_id:
            raise GDriveError("Drive 完成回應缺少檔案 id")
        if got and size and got != size:
            # Corrupt/short upload — drop it and start over.
            try:
                await gdrive_client.trash(auth, file_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("trash of bad upload %s failed: %s", file_id, exc)
            async with SessionLocal() as session:
                await session.execute(
                    update(GDriveTransfer).where(GDriveTransfer.id == rid).values(session_uri="", bytes_uploaded=0)
                )
                await session.commit()
            raise GDriveError(f"大小不符(Drive {got} ≠ PikPak {size})")

        video = is_video(name)
        if video:
            try:
                await gdrive_client.make_public(auth, file_id)
            except Exception as exc:  # noqa: BLE001 — playback degrades to a link
                logger.warning("make_public %s failed: %s", file_id, exc)
        code_folder_id = folder_id
        try:
            code_folder_id = await gdrive_client.ensure_path(auth, code_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("code folder lookup failed for %s: %s", code_dir, exc)

        async with SessionLocal() as session:
            job = await session.get(GDriveJob, job_id)
            session.add(
                GDriveLocation(
                    code=code,
                    account_id=account_id,
                    job_id=job_id,
                    listing_kind=job.listing_kind if job else "studio",
                    studio_id=job.studio_id if job else "",
                    drive_file_id=file_id,
                    drive_folder_id=folder_id,
                    code_folder_id=code_folder_id,
                    path=drive_path_for(path),
                    name=name,
                    size=got or size,
                    is_video=video,
                )
            )
            await session.execute(
                update(GDriveTransfer)
                .where(GDriveTransfer.id == rid)
                .values(
                    status="done",
                    message="完成",
                    drive_file_id=file_id,
                    bytes_uploaded=size,
                    session_uri="",
                    finished_at=datetime.utcnow(),
                )
            )
            await session.commit()
        studio_index.invalidate()

    async def _park(self, rid: int, reason: str, wait_seconds: float) -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(GDriveTransfer)
                .where(GDriveTransfer.id == rid, GDriveTransfer.status == "running")
                .values(
                    status="pending",
                    message=f"暫停:{reason}",
                    next_retry_at=datetime.utcnow() + timedelta(seconds=max(30.0, wait_seconds)),
                )
            )
            await session.commit()

    async def _fail(self, rid: int, message: str) -> None:
        async with SessionLocal() as session:
            row = await session.get(GDriveTransfer, rid)
            if row is None or row.status not in ("running", "pending"):
                return
            row.status = "failed"
            row.message = message[:2000]
            row.finished_at = datetime.utcnow()
            await session.commit()

    async def _fail_or_retry(self, rid: int, message: str) -> None:
        max_attempts = max(1, settings.gdrive_transfer_max_attempts)
        async with SessionLocal() as session:
            row = await session.get(GDriveTransfer, rid)
            if row is None or row.status not in ("running", "pending"):
                return
            row.attempts = (row.attempts or 0) + 1
            if row.attempts < max_attempts:
                delay = retry_delay(row.attempts, settings.gdrive_transfer_retry_base_seconds)
                row.status = "pending"
                row.next_retry_at = datetime.utcnow() + timedelta(seconds=delay)
                row.message = f"{message}(第 {row.attempts} 次失敗,約 {delay} 秒後續傳)"[:2000]
            else:
                row.status = "failed"
                row.message = f"{message}(已重試 {row.attempts - 1} 次)"[:2000]
                row.finished_at = datetime.utcnow()
            await session.commit()

    # ---------- job completion ----------

    async def _maybe_finish_job(self, job_id: int) -> None:
        lock = self._job_locks.setdefault(job_id, asyncio.Lock())
        async with lock:
            async with SessionLocal() as session:
                job = await session.get(GDriveJob, job_id)
                if job is None or job.status != "transferring":
                    return
                counts = dict(
                    (
                        await session.execute(
                            select(GDriveTransfer.status, func.count())
                            .where(GDriveTransfer.job_id == job_id)
                            .group_by(GDriveTransfer.status)
                        )
                    ).all()
                )
                outcome = job_outcome(counts)
                if outcome is None:
                    return
                job.status = outcome
                job.finished_at = datetime.utcnow()
                name, delete_source = job.studio_name, job.delete_source
                if outcome == "partial":
                    job.message = (
                        f"{counts.get('failed', 0)} 個檔案失敗、{counts.get('cancelled', 0)} 個取消;"
                        "PikPak 原檔保留"
                    )
                await session.commit()
            if outcome == "partial":
                webhook_queue.enqueue_nowait(
                    f"⚠️ Google Drive 搬移未完成:{name}({counts.get('failed', 0)} 個檔案失敗)",
                    event="gdrive_failed",
                )
                return
            msg = f"✅ Google Drive 搬移完成:{name}"
            if delete_source:
                trashed = await self.trash_source(job_id)
                msg += ",PikPak 原檔已丟垃圾桶" if trashed else ",但 PikPak 原檔刪除失敗(可在佇列頁重試)"
            webhook_queue.enqueue_nowait(msg, event="gdrive_done")

    async def trash_source(self, job_id: int) -> bool:
        """Trash the PikPak leaves of a finished job. Safe to re-run.

        ``done`` job: every leaf. ``partial`` job (explicit request only —
        the automatic path never calls this for one): just the leaves whose
        rows are all ``done``, i.e. already size-verified on Drive. A leaf
        shared with a failed/cancelled row is kept, so nothing that has not
        reached Drive is ever trashed. Without this a single permanently
        failing file (e.g. its PikPak id went stale) pins every other
        already-moved original on PikPak forever.
        """
        async with SessionLocal() as session:
            job = await session.get(GDriveJob, job_id)
            if job is None or job.status not in ("done", "partial"):
                return False
            partial = job.status == "partial"
            rows = (
                await session.execute(
                    select(GDriveTransfer.source_leaf_id, GDriveTransfer.code, GDriveTransfer.status)
                    .where(GDriveTransfer.job_id == job_id)
                )
            ).all()
        if not partial and any(st != "done" for _, _, st in rows):
            return False
        if partial and any(st not in ("done", "failed", "cancelled") for _, _, st in rows):
            return False  # something is still queued/running — not settled
        kept_leaves = {leaf for leaf, _, st in rows if leaf and st != "done"}
        leaf_ids = sorted({leaf for leaf, _, st in rows if leaf and st == "done"} - kept_leaves)
        codes = sorted({c for leaf, c, st in rows if c and st == "done" and leaf not in kept_leaves})
        if partial and not leaf_ids:
            return False
        try:
            for i in range(0, len(leaf_ids), 100):
                await pikpak_service.trash_files(leaf_ids[i : i + 100])
        except Exception as exc:  # noqa: BLE001
            logger.warning("gdrive job %s: PikPak trash failed: %s", job_id, exc)
            await self._set_job(job_id, source_state="trash_failed", message=f"PikPak 原檔刪除失敗: {exc}")
            return False
        if partial:
            kept = sum(1 for _, _, st in rows if st != "done")
            await self._set_job(
                job_id,
                source_state="trashed",
                message=f"已搬完的 {len(leaf_ids)} 個 PikPak 原檔已丟垃圾桶;{kept} 個未搬完的檔案原檔保留",
            )
        else:
            await self._set_job(job_id, source_state="trashed", message="PikPak 原檔已丟垃圾桶")
        try:
            await presence_index.refresh_codes(codes, exclude_ids=set(leaf_ids))
        except Exception as exc:  # noqa: BLE001
            logger.warning("presence refresh after gdrive move failed: %s", exc)
            presence_index.invalidate()
        studio_index.invalidate()
        listing_codes.invalidate()
        try:
            from . import missing as missing_svc  # local: avoid import cycle

            missing_svc.invalidate_result_caches()
        except Exception as exc:  # noqa: BLE001
            logger.warning("missing cache invalidate after gdrive move failed: %s", exc)
        return True

    # ---------- caller actions ----------

    async def retry_all(self) -> int:
        """Requeue every failed/cancelled file of every unfinished job
        (``partial`` / ``error`` / ``cancelled``). Returns files requeued."""
        async with SessionLocal() as session:
            job_ids = (
                await session.execute(
                    select(GDriveJob.id).where(GDriveJob.status.in_(("partial", "error", "cancelled")))
                )
            ).scalars().all()
        total = 0
        for jid in job_ids:
            total += await self.retry_job(jid)
        return total

    async def retry_job(self, job_id: int) -> int:
        async with SessionLocal() as session:
            res = await session.execute(
                update(GDriveTransfer)
                .where(GDriveTransfer.job_id == job_id, GDriveTransfer.status.in_(("failed", "cancelled")))
                .values(status="pending", attempts=0, next_retry_at=None, message="已重新排隊", finished_at=None)
            )
            n = res.rowcount or 0
            if n:
                await session.execute(
                    update(GDriveJob)
                    .where(GDriveJob.id == job_id)
                    .values(status="transferring", finished_at=None, message="重試中")
                )
            await session.commit()
        self.notify()
        return n

    async def retry(self, rid: int) -> bool:
        async with SessionLocal() as session:
            row = await session.get(GDriveTransfer, rid)
            if row is None or row.status not in ("failed", "cancelled"):
                return False
            row.status = "pending"
            row.attempts = 0
            row.next_retry_at = None
            row.message = "已重新排隊"
            row.finished_at = None
            job = await session.get(GDriveJob, row.job_id)
            if job is not None and job.status in ("partial", "cancelled"):
                job.status = "transferring"
                job.finished_at = None
            await session.commit()
        self.notify()
        return True

    async def cancel_job(self, job_id: int) -> int:
        task = self._scans.pop(job_id, None)
        if task is not None:
            task.cancel()
        async with SessionLocal() as session:
            res = await session.execute(
                update(GDriveTransfer)
                .where(GDriveTransfer.job_id == job_id, GDriveTransfer.status.in_(("pending", "running")))
                .values(status="cancelled", message="已取消", finished_at=datetime.utcnow())
            )
            await session.execute(
                update(GDriveJob)
                .where(GDriveJob.id == job_id, GDriveJob.status.in_(ACTIVE_JOB_STATES))
                .values(status="cancelled", finished_at=datetime.utcnow(), message="已取消;PikPak 原檔保留")
            )
            await session.commit()
        self.notify()
        return res.rowcount or 0

    async def cleanup(self) -> int:
        """Drop finished jobs (done/cancelled/error) and their transfer
        rows. Locations are kept — they ARE the Drive index."""
        async with SessionLocal() as session:
            ids = (
                await session.execute(
                    select(GDriveJob.id).where(GDriveJob.status.in_(("done", "cancelled", "error")))
                )
            ).scalars().all()
            if ids:
                await session.execute(delete(GDriveTransfer).where(GDriveTransfer.job_id.in_(ids)))
                await session.execute(delete(GDriveJob).where(GDriveJob.id.in_(ids)))
            await session.commit()
        return len(ids)

    async def status(self) -> dict:
        async with SessionLocal() as session:
            counts = dict(
                (
                    await session.execute(
                        select(GDriveTransfer.status, func.count()).group_by(GDriveTransfer.status)
                    )
                ).all()
            )
        policy = await gdrive_policy.load()
        return {
            "pending": int(counts.get("pending", 0)),
            "running": int(counts.get("running", 0)),
            "done": int(counts.get("done", 0)),
            "failed": int(counts.get("failed", 0)),
            "cancelled": int(counts.get("cancelled", 0)),
            "inflight": len(self._inflight),
            "concurrency": settings.gdrive_transfer_concurrency,
            "policy": policy.to_public(),
            "pikpak_blocked_reason": (pikpak_unavailable() or ("", 0))[0],
        }


gdrive_transfer_queue = GDriveTransferQueue()
