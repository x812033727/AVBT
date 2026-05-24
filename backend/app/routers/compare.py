import json

from fastapi import APIRouter, Body
from fastapi.responses import StreamingResponse

from ..services.duplicates import find_duplicates_stream

router = APIRouter(prefix="/api/compare", tags=["compare"])


@router.post("/duplicates/stream")
async def duplicates_stream(payload: dict = Body(...)):
    """Stream NDJSON events comparing a PikPak folder subtree against a
    pCloud folder subtree, reporting the JAV codes present in BOTH.

    Body: ``{pikpak_folder_id: str = "", pcloud_folder_id: str = "0",
            max_depth?: int = 8, cap?: int = 20000}``.

    PikPak's drive root is the empty string; pCloud's is ``"0"``.
    Read-only — nothing is mutated on either cloud.
    """
    pikpak_folder_id = str(payload.get("pikpak_folder_id") or "")
    pcloud_folder_id = str(payload.get("pcloud_folder_id") or "0").strip() or "0"
    max_depth = int(payload.get("max_depth") or 8)
    cap = int(payload.get("cap") or 20000)

    async def gen():
        try:
            async for event in find_duplicates_stream(
                pikpak_folder_id,
                pcloud_folder_id,
                max_depth=max_depth,
                cap=cap,
            ):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")
