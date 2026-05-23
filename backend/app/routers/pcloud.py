"""HTTP API for PikPak → pCloud transfer.

Endpoints fall into four buckets:

- Auth: ``/login`` (username+password OR access_token), ``/status``,
  ``/logout``.
- Folder browser: ``/folders`` (list children), ``/folders/ensure``
  (create a path on demand).
- Transfer: ``/transfers`` POST (enqueue files / a folder) + GET (list
  current jobs) + per-row retry / cancel / cleanup.
- Queue: ``/queue`` snapshot.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query
from sqlalchemy import desc, func, select

from ..database import SessionLocal
from ..models import PCloudTransfer
from ..schemas import (
    PCloudEnqueueResult,
    PCloudFolderEntry,
    PCloudFolderListing,
    PCloudLogin,
    PCloudStatus,
    PCloudTransferOut,
    PCloudTransferPage,
    PCloudTransferRequest,
)
from ..config import settings
from ..services.pcloud import PCloudError, pcloud_service
from ..services.pcloud_transfer import pcloud_transfer_queue
from ..services.pikpak import PikPakError, pikpak_service

router = APIRouter(prefix="/api/pcloud", tags=["pcloud"])


def _wrap(exc: Exception) -> HTTPException:
    if isinstance(exc, PCloudError):
        return HTTPException(status_code=400, detail=f"pCloud: {exc}")
    if isinstance(exc, PikPakError):
        return HTTPException(status_code=400, detail=f"PikPak: {exc}")
    return HTTPException(status_code=502, detail=f"傳輸錯誤: {exc}")


# ---------- auth ----------

@router.post("/login", response_model=PCloudStatus)
async def login(payload: PCloudLogin):
    try:
        await pcloud_service.login(
            username=payload.username,
            password=payload.password,
            access_token=payload.access_token,
        )
        return PCloudStatus(**pcloud_service.status())
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.get("/status", response_model=PCloudStatus)
async def status():
    return PCloudStatus(**pcloud_service.status())


@router.post("/logout")
async def logout():
    pcloud_service.logout()
    return {"ok": True}


# ---------- folder browser ----------

@router.get("/folders", response_model=PCloudFolderListing)
async def list_folder(folder_id: int = Query(0, ge=0)):
    """List children of a pCloud folder. ``folder_id=0`` = root."""
    try:
        meta = await pcloud_service.list_folder(folder_id)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc

    entries: list[PCloudFolderEntry] = []
    for c in meta.get("contents") or []:
        is_folder = bool(c.get("isfolder"))
        entries.append(
            PCloudFolderEntry(
                folder_id=int(c.get("folderid") or 0) if is_folder else 0,
                file_id=int(c.get("fileid") or 0) if not is_folder else 0,
                name=c.get("name") or "",
                is_folder=is_folder,
                size=int(c.get("size") or 0),
            )
        )
    entries.sort(key=lambda e: (not e.is_folder, e.name.lower()))
    return PCloudFolderListing(
        folder_id=int(meta.get("folderid") or folder_id or 0),
        path=meta.get("path") or "/",
        parent_folder_id=meta.get("parentfolderid"),
        entries=entries,
    )


@router.post("/folders/ensure")
async def ensure_folder(path: str = Body(..., embed=True)):
    """Walk-create ``path`` (e.g. ``/AVBT/From PikPak/SSIS-001``). Returns
    the resolved folder id and the canonical path."""
    try:
        folder_id = await pcloud_service.ensure_path(path)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    return {"folder_id": folder_id, "path": path or "/"}


# ---------- transfer ----------

def _serialize(row: PCloudTransfer) -> PCloudTransferOut:
    return PCloudTransferOut(
        id=row.id,
        parent_id=row.parent_id,
        pikpak_file_id=row.pikpak_file_id,
        pikpak_name=row.pikpak_name,
        pikpak_size=row.pikpak_size,
        pikpak_path=row.pikpak_path,
        pcloud_folder_id=row.pcloud_folder_id,
        pcloud_folder_path=row.pcloud_folder_path,
        pcloud_upload_id=row.pcloud_upload_id,
        pcloud_file_id=row.pcloud_file_id,
        status=row.status,
        message=row.message,
        bytes_downloaded=row.bytes_downloaded,
        delete_source=row.delete_source,
        created_at=row.created_at,
        updated_at=row.updated_at,
        finished_at=row.finished_at,
    )


async def _resolve_destination_path(folder: str) -> str:
    """Pick the destination path: caller-supplied wins, then settings
    default, then root."""
    if folder and folder.strip():
        path = folder.strip()
    else:
        path = (settings.pcloud_default_folder or "/").strip() or "/"
    if not path.startswith("/"):
        path = "/" + path
    return path


@router.post("/transfers", response_model=PCloudEnqueueResult)
async def enqueue_transfer(payload: PCloudTransferRequest):
    """Enqueue PikPak → pCloud transfers.

    Two modes:
    - ``pikpak_file_ids``: one row per file, all into the same destination
      folder.
    - ``pikpak_folder_id``: walk the folder recursively. When
      ``preserve_subfolders=true`` (default), subfolders are mirrored
      under the destination.
    """
    if not payload.pikpak_file_ids and not payload.pikpak_folder_id:
        raise HTTPException(
            status_code=400,
            detail="請指定 pikpak_file_ids 或 pikpak_folder_id",
        )

    base_path = await _resolve_destination_path(payload.folder)

    try:
        base_folder_id = await pcloud_service.ensure_path(base_path)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc

    # ---- file-list mode ----
    if payload.pikpak_file_ids:
        files: list[dict] = []
        for fid in payload.pikpak_file_ids:
            try:
                # PikPak doesn't have a clean "stat one file" call we can
                # rely on here. We fetch via the parent listing only if
                # we need a size; for the transfer itself, name is fetched
                # by file_links() on the way through but we still need a
                # human name to record. Cheap approach: ask PikPak for a
                # link, which fails fast on bad ids.
                _ = await pikpak_service.file_links(fid)
            except Exception as exc:  # noqa: BLE001
                raise _wrap(exc) from exc
            files.append({"file_id": fid, "name": "", "size": 0, "source_path": ""})

        # Best-effort enrich names: list the first parent (if available).
        # For now, leave name blank — the worker tolerates it (pCloud
        # will pick filename from the URL). Callers that care set name
        # via the bulk endpoint below.
        new_ids = await pcloud_transfer_queue.enqueue_files(
            files,
            pcloud_folder_id=base_folder_id,
            pcloud_folder_path=base_path,
            delete_source=payload.delete_source,
        )
        return PCloudEnqueueResult(
            enqueued=len(new_ids),
            transfer_ids=new_ids,
            folder_path=base_path,
            folder_id=base_folder_id,
        )

    # ---- recursive folder mode ----
    try:
        walked = await pcloud_transfer_queue.walk_pikpak_folder(
            payload.pikpak_folder_id
        )
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc

    if not walked:
        raise HTTPException(status_code=400, detail="此 PikPak 資料夾為空")

    # Pre-create every subfolder once so the worker doesn't redundantly
    # ensure the same path per-file.
    subfolder_id_cache: dict[str, int] = {"": base_folder_id}
    if payload.preserve_subfolders:
        all_rel_dirs = sorted({w["rel_dir"] for w in walked if w["rel_dir"]})
        for rel in all_rel_dirs:
            full = f"{base_path.rstrip('/')}/{rel}"
            try:
                fid = await pcloud_service.ensure_path(full)
            except Exception as exc:  # noqa: BLE001
                raise _wrap(exc) from exc
            subfolder_id_cache[rel] = fid

    # Group files by destination folder so each enqueue batch shares
    # one pcloud_folder_id / path; the parent_id chains them.
    enqueued_total = 0
    new_ids_all: list[int] = []

    # Build a virtual "parent" row so the UI can collapse — implement
    # as a no-op placeholder later if you want a true parent. For now
    # leave parent_id=NULL; group via folder_path on the frontend.
    by_rel: dict[str, list[dict]] = {}
    for w in walked:
        rel = w["rel_dir"] if payload.preserve_subfolders else ""
        by_rel.setdefault(rel, []).append({
            "file_id": w["file_id"],
            "name": w["name"],
            "size": w["size"],
            "source_path": w["rel_dir"],
        })

    for rel, files in by_rel.items():
        dest_fid = subfolder_id_cache.get(rel, base_folder_id)
        dest_path = (
            f"{base_path.rstrip('/')}/{rel}" if rel else base_path
        )
        ids = await pcloud_transfer_queue.enqueue_files(
            files,
            pcloud_folder_id=dest_fid,
            pcloud_folder_path=dest_path,
            delete_source=payload.delete_source,
        )
        new_ids_all.extend(ids)
        enqueued_total += len(ids)

    return PCloudEnqueueResult(
        enqueued=enqueued_total,
        transfer_ids=new_ids_all,
        folder_path=base_path,
        folder_id=base_folder_id,
    )


@router.get("/transfers", response_model=PCloudTransferPage)
async def list_transfers(
    status: str = Query("", description="pending/running/done/failed/cancelled"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    async with SessionLocal() as session:
        # Counts
        count_rows = (
            await session.execute(
                select(PCloudTransfer.status, func.count())
                .group_by(PCloudTransfer.status)
            )
        ).all()
        counts = {s: int(n) for s, n in count_rows}
        total = sum(counts.values())

        stmt = select(PCloudTransfer)
        if status:
            stmt = stmt.where(PCloudTransfer.status == status)
        stmt = stmt.order_by(desc(PCloudTransfer.id)).limit(limit).offset(offset)
        rows = (await session.execute(stmt)).scalars().all()

    items = [_serialize(r) for r in rows]
    return PCloudTransferPage(
        items=items,
        total=total,
        pending=counts.get("pending", 0),
        running=counts.get("running", 0),
        done=counts.get("done", 0),
        failed=counts.get("failed", 0),
    )


@router.post("/transfers/{transfer_id}/retry")
async def retry_transfer(transfer_id: int):
    ok = await pcloud_transfer_queue.retry(transfer_id)
    if not ok:
        raise HTTPException(status_code=400, detail="此任務狀態無法重試")
    return {"ok": True}


@router.post("/transfers/{transfer_id}/cancel")
async def cancel_transfer(transfer_id: int):
    ok = await pcloud_transfer_queue.cancel(transfer_id)
    if not ok:
        raise HTTPException(status_code=400, detail="此任務無法取消")
    return {"ok": True}


@router.post("/transfers/cleanup")
async def cleanup_transfers(keep_failed: bool = Body(True, embed=True)):
    """Delete rows in terminal states. Pass ``keep_failed=false`` to
    also drop the failed ones."""
    n = await pcloud_transfer_queue.cleanup(keep_failed=keep_failed)
    return {"deleted": n}


@router.get("/queue")
async def queue_status():
    return await pcloud_transfer_queue.status()
