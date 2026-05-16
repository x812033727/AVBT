from fastapi import APIRouter, HTTPException, Query

from ..scrapers import javbus as scraper
from ..scrapers.javbus import JavbusBlocked
from ..schemas import MovieDetail, SearchResult, SendAllOptions, SendAllResult
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


@router.post("/star/{star_id}/send-all", response_model=SendAllResult)
async def star_send_all(star_id: str, options: SendAllOptions):
    try:
        return await bulk.send_all("star", star_id, options)
    except JavbusBlocked as exc:
        raise HTTPException(status_code=451, detail=str(exc)) from exc


@router.post("/genre/{genre_id}/send-all", response_model=SendAllResult)
async def genre_send_all(genre_id: str, options: SendAllOptions):
    try:
        return await bulk.send_all("genre", genre_id, options)
    except JavbusBlocked as exc:
        raise HTTPException(status_code=451, detail=str(exc)) from exc
