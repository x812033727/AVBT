import json

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..schemas import PCloudFile, PCloudLogin, PCloudQuota
from ..services.jav_code import extract_jav_code, is_video
from ..services.pcloud import PCloudError, pcloud_service


router = APIRouter(prefix="/api/pcloud", tags=["pcloud"])


def _wrap(exc: Exception) -> HTTPException:
    code = 400 if isinstance(exc, PCloudError) else 502
    return HTTPException(status_code=code, detail=f"pCloud 錯誤: {exc}")


@router.post("/login")
async def login(payload: PCloudLogin):
    if not payload.username or not payload.password:
        raise HTTPException(status_code=400, detail="請填入帳號與密碼")
    try:
        return await pcloud_service.login(payload.username, payload.password)
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
