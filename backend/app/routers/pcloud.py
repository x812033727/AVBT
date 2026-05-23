import json

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, func, select

from ..config import settings
from ..database import SessionLocal
from ..models import PCloudTransfer
from ..schemas import (
    PCloudEnqueueResult,
    PCloudFile,
    PCloudLogin,
    PCloudQuota,
    PCloudTransferOut,
    PCloudTransferPage,
    PCloudTransferRequest,
)
from ..services.jav_code import extract_jav_code, is_video
from ..services.pcloud import PCloudError, pcloud_service
from ..services.pcloud_transfer import pcloud_transfer_queue
from ..services.pikpak import PikPakError, pikpak_service


router = APIRouter(prefix="/api/pcloud", tags=["pcloud"])


def _wrap(exc: Exception) -> HTTPException:
    # PCloudError messages already include "pCloud 錯誤" prefix; passing
    # them through verbatim avoids the doubled-prefix output that made
    # past errors read like "pCloud 錯誤: pCloud 錯誤 (2000): ...".
    if isinstance(exc, PCloudError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, PikPakError):
        return HTTPException(status_code=400, detail=f"PikPak 錯誤: {exc}")
    return HTTPException(status_code=502, detail=f"pCloud 錯誤: {exc}")


@router.post("/login")
async def login(payload: PCloudLogin):
    has_token = bool(payload.access_token)
    if not has_token and (not payload.username or not payload.password):
        raise HTTPException(
            status_code=400, detail="請填入帳號與密碼,或提供 access token"
        )
    try:
        return await pcloud_service.login(
            username=payload.username,
            password=payload.password,
            access_token=payload.access_token,
        )
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.post("/logout")
async def logout():
    pcloud_service.logout()
    return {"ok": True}


@router.get("/status")
async def status():
    info = pcloud_service.status()
    if info["logged_in"]:
        try:
            quota = await pcloud_service.quota()
            info["quota"] = quota.model_dump()
        except Exception as exc:  # noqa: BLE001
            info["quota_error"] = str(exc)
    return info


@router.get("/quota", response_model=PCloudQuota)
async def quota():
    try:
        return await pcloud_service.quota()
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.get("/files", response_model=list[PCloudFile])
async def list_files(parent_id: str = "0"):
    try:
        return await pcloud_service.list_files(parent_id=parent_id)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.get("/files/search", response_model=list[PCloudFile])
async def search_files(q: str = Query(..., min_length=1), parent_id: str = "0"):
    try:
        return await pcloud_service.search_files(q, parent_id=parent_id)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.get("/files/{file_id}/url")
async def file_url(file_id: str):
    try:
        links = await pcloud_service.file_links(file_id)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    return {"url": links["download_url"], **links}


@router.get("/files/stats")
async def folder_stats(parent_id: str = "0"):
    """Lightweight aggregate for a folder's direct children. Mirrors the
    PikPak version but without the OfflineTaskLog cross-check (pCloud
    isn't part of the archiver flow).
    """
    try:
        children, partial = await pcloud_service.list_all_files(parent_id)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc

    total_files = 0
    total_folders = 0
    total_size = 0
    video_count = 0
    video_size = 0
    coded_count = 0
    for c in children:
        if c.kind == "folder":
            total_folders += 1
        else:
            total_files += 1
            total_size += int(c.size or 0)
            if is_video(c.name):
                video_count += 1
                video_size += int(c.size or 0)
            if extract_jav_code(c.name):
                coded_count += 1

    return {
        "total_files": total_files,
        "total_folders": total_folders,
        "total_size": total_size,
        "video_count": video_count,
        "video_size": video_size,
        "coded_count": coded_count,
        "partial": partial,
    }


@router.post("/files/trash")
async def trash_files(ids: list[str] = Body(..., embed=True)):
    if not ids:
        raise HTTPException(status_code=400, detail="未指定要刪除的項目")
    try:
        return await pcloud_service.trash_files(ids)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.post("/files/move")
async def move_files(
    file_ids: list[str] = Body(..., embed=True),
    target_folder_id: str = Body("0", embed=True),
):
    if not file_ids:
        raise HTTPException(status_code=400, detail="未指定要移動的項目")
    try:
        return await pcloud_service.move_files(file_ids, target_folder_id)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.post("/files/rename")
async def rename_file(
    file_id: str = Body(..., embed=True),
    new_name: str = Body(..., embed=True),
):
    try:
        await pcloud_service.rename_file(file_id, new_name)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    return {"ok": True}


@router.post("/folders/create", response_model=PCloudFile)
async def create_folder(
    parent_id: str = Body("0", embed=True),
    name: str = Body(..., embed=True),
):
    try:
        return await pcloud_service.create_folder(parent_id, name)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.post("/files/cleanup/stream")
async def cleanup_folder_stream(payload: dict = Body(...)):
    """Stream NDJSON cleanup events for the direct children of a folder.

    Body: ``{folder_id: str, dry_run: bool=True}``. Only renames items
    whose name encodes a recognisable JAV code; folders are not
    flattened (pCloud isn't a BT download target).
    """
    folder_id = str(payload.get("folder_id") or "0").strip() or "0"
    dry_run = bool(payload.get("dry_run", True))

    async def gen():
        try:
            async for event in pcloud_service.cleanup_folder_stream(
                folder_id, dry_run=dry_run
            ):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ---------- PikPak → pCloud transfer queue ----------

@router.post("/folders/ensure")
async def ensure_folder(path: str = Body(..., embed=True)):
    """Walk-create ``path`` (e.g. ``/AVBT/From PikPak/SSIS-001``) and
    return the resolved folder id. Used by the transfer modal to
    materialise the destination before submitting."""
    try:
        folder_id = await pcloud_service.ensure_path(path)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    return {"folder_id": folder_id, "path": path or "/"}


def _serialize_transfer(row: PCloudTransfer) -> PCloudTransferOut:
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
    path = (folder or "").strip()
    if not path:
        path = (settings.pcloud_default_folder or "/").strip() or "/"
    if not path.startswith("/"):
        path = "/" + path
    return path


@router.post("/transfers", response_model=PCloudEnqueueResult)
async def enqueue_transfer(payload: PCloudTransferRequest):
    """Enqueue PikPak → pCloud transfers.

    Two modes:
    - ``pikpak_file_ids``: one row per file into the same destination.
    - ``pikpak_folder_id``: walk the folder recursively; when
      ``preserve_subfolders=true``, subfolders are mirrored under the
      destination.
    """
    if not payload.pikpak_file_ids and not payload.pikpak_folder_id:
        raise HTTPException(
            status_code=400, detail="請指定 pikpak_file_ids 或 pikpak_folder_id"
        )

    base_path = await _resolve_destination_path(payload.folder)
    try:
        base_folder_id = await pcloud_service.ensure_path(base_path)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc

    if payload.pikpak_file_ids:
        files: list[dict] = []
        for fid in payload.pikpak_file_ids:
            # Validate the PikPak file exists by asking for its link.
            # Cheap and surfaces a clear error before we enqueue.
            try:
                await pikpak_service.file_links(fid)
            except Exception as exc:  # noqa: BLE001
                raise _wrap(exc) from exc
            files.append({"file_id": fid, "name": "", "size": 0, "source_path": ""})
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

    # Recursive folder mode
    try:
        walked = await pcloud_transfer_queue.walk_pikpak_folder(
            payload.pikpak_folder_id
        )
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    if not walked:
        raise HTTPException(status_code=400, detail="此 PikPak 資料夾為空")

    # Pre-create every subfolder once.
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

    new_ids_all: list[int] = []
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
        dest_path = f"{base_path.rstrip('/')}/{rel}" if rel else base_path
        ids = await pcloud_transfer_queue.enqueue_files(
            files,
            pcloud_folder_id=dest_fid,
            pcloud_folder_path=dest_path,
            delete_source=payload.delete_source,
        )
        new_ids_all.extend(ids)

    return PCloudEnqueueResult(
        enqueued=len(new_ids_all),
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

    return PCloudTransferPage(
        items=[_serialize_transfer(r) for r in rows],
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
    n = await pcloud_transfer_queue.cleanup(keep_failed=keep_failed)
    return {"deleted": n}


@router.get("/queue")
async def queue_status():
    return await pcloud_transfer_queue.status()
