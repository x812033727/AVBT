import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..scrapers import javbus as scraper
from ..scrapers.javbus import JavbusBlocked
from ..schemas import (
    MovieDetail,
    SearchResult,
    SendAllOptions,
    SendAllResult,
    StarProfile,
)
from ..services import bulk

router = APIRouter(prefix="/api/javbus", tags=["javbus"])


@router.get("/search", response_model=SearchResult)
async def search(
    q: str = Query(..., min_length=1, description="番號 / 關鍵字 / 女優名"),
    page: int = Query(1, ge=1),
    uncensored: bool = Query(False),
):
    try:
        return await scraper.search(q, page=page, uncensored=uncensored)
    except JavbusBlocked as exc:
        raise HTTPException(status_code=451, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"JavBus 搜尋失敗: {exc}") from exc


@router.get("/movie/{code}", response_model=MovieDetail)
async def movie_detail(code: str):
    try:
        detail = await scraper.fetch_detail(code)
    except JavbusBlocked as exc:
        raise HTTPException(status_code=451, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"JavBus 詳細頁失敗: {exc}") from exc
    if not detail.title:
        raise HTTPException(status_code=404, detail=f"找不到番號 {code}")
    return detail


@router.get("/star/{star_id}", response_model=SearchResult)
async def actress_movies(
    star_id: str,
    page: int = Query(1, ge=1),
    uncensored: bool = Query(False),
):
    try:
        return await scraper.fetch_star(star_id, page=page, uncensored=uncensored)
    except JavbusBlocked as exc:
        raise HTTPException(status_code=451, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"JavBus 女優頁失敗: {exc}") from exc


@router.get("/star/{star_id}/profile", response_model=StarProfile | None)
async def actress_profile(star_id: str, uncensored: bool = Query(False)):
    try:
        return await scraper.fetch_star_profile(star_id, uncensored=uncensored)
    except JavbusBlocked as exc:
        raise HTTPException(status_code=451, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"女優 profile 失敗: {exc}") from exc


@router.get("/genre/{genre_id}", response_model=SearchResult)
async def genre_movies(
    genre_id: str,
    page: int = Query(1, ge=1),
    uncensored: bool = Query(False),
):
    try:
        return await scraper.fetch_genre(genre_id, page=page, uncensored=uncensored)
    except JavbusBlocked as exc:
        raise HTTPException(status_code=451, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"JavBus 類別頁失敗: {exc}") from exc


def _stream_response(kind: str, slug: str, options: SendAllOptions) -> StreamingResponse:
    async def gen():
        try:
            async for event in bulk.send_all_stream(kind, slug, options):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except JavbusBlocked as exc:
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.post("/star/{star_id}/send-all", response_model=SendAllResult)
async def star_send_all(star_id: str, options: SendAllOptions):
    try:
        return await bulk.send_all("star", star_id, options)
    except JavbusBlocked as exc:
        raise HTTPException(status_code=451, detail=str(exc)) from exc


@router.post("/star/{star_id}/send-all/stream")
async def star_send_all_stream(star_id: str, options: SendAllOptions):
    return _stream_response("star", star_id, options)


@router.post("/genre/{genre_id}/send-all", response_model=SendAllResult)
async def genre_send_all(genre_id: str, options: SendAllOptions):
    try:
        return await bulk.send_all("genre", genre_id, options)
    except JavbusBlocked as exc:
        raise HTTPException(status_code=451, detail=str(exc)) from exc


@router.post("/genre/{genre_id}/send-all/stream")
async def genre_send_all_stream(genre_id: str, options: SendAllOptions):
    return _stream_response("genre", genre_id, options)


# ---------- studio / label / series / director ----------
#
# JavBus exposes /studio/{slug}, /label/{slug}, /series/{slug},
# /director/{slug}. Each behaves identically to /genre at the HTML
# level, so we register the same three endpoints (list, send-all,
# send-all-stream) per kind via a small factory.

_EXTRA_KINDS = {
    "studio": "製作商",
    "label": "發行商",
    "series": "系列",
    "director": "導演",
}


def _register_extra_kind(kind: str, label: str) -> None:
    list_path = f"/{kind}/{{slug}}"
    send_path = f"/{kind}/{{slug}}/send-all"
    stream_path = f"/{kind}/{{slug}}/send-all/stream"

    @router.get(list_path, response_model=SearchResult, name=f"list_{kind}")
    async def listing(
        slug: str,
        page: int = Query(1, ge=1),
        uncensored: bool = Query(False),
    ):
        try:
            return await scraper.fetch_listing(
                kind, slug, page=page, uncensored=uncensored
            )
        except JavbusBlocked as exc:
            raise HTTPException(status_code=451, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502, detail=f"JavBus {label}頁失敗: {exc}"
            ) from exc

    @router.post(send_path, response_model=SendAllResult, name=f"send_all_{kind}")
    async def send_all(slug: str, options: SendAllOptions):
        try:
            return await bulk.send_all(kind, slug, options)
        except JavbusBlocked as exc:
            raise HTTPException(status_code=451, detail=str(exc)) from exc

    @router.post(stream_path, name=f"send_all_stream_{kind}")
    async def send_all_stream(slug: str, options: SendAllOptions):
        return _stream_response(kind, slug, options)


for _kind, _label in _EXTRA_KINDS.items():
    _register_extra_kind(_kind, _label)
