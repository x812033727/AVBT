"""Image proxy.

The JavBus cover/avatar CDN refuses requests from the browser due to
hot-link protection and (in our test env) age-gate redirects. We proxy
every image through this endpoint so the browser sees a same-origin
request and we set the Referer the upstream expects.
"""

from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from ..config import settings
from ..scrapers.javbus import DEFAULT_COOKIES, USER_AGENT

router = APIRouter(prefix="/api/img", tags=["img"])


_client_singleton: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


def _safe_url(url: str) -> bool:
    """Reject anything that isn't http(s) or that points at a private IP."""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False
        host = p.hostname or ""
        if not host:
            return False
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
                return False
        except ValueError:
            pass  # hostname; assume public
        return True
    except Exception:
        return False


def _build_client() -> httpx.AsyncClient:
    kwargs = dict(
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Referer": settings.javbus_base_url.rstrip("/") + "/",
        },
        cookies=DEFAULT_COOKIES,
        follow_redirects=True,
        timeout=20.0,
    )
    if settings.http_proxy:
        kwargs["proxy"] = settings.http_proxy
    return httpx.AsyncClient(**kwargs)


async def _get_client() -> httpx.AsyncClient:
    global _client_singleton
    if _client_singleton is None or _client_singleton.is_closed:
        async with _client_lock:
            if _client_singleton is None or _client_singleton.is_closed:
                _client_singleton = _build_client()
    return _client_singleton


async def aclose_client() -> None:
    global _client_singleton
    cli, _client_singleton = _client_singleton, None
    if cli is not None and not cli.is_closed:
        try:
            await cli.aclose()
        except Exception:  # noqa: BLE001
            pass


@router.get("/proxy")
async def proxy_image(url: str = Query(..., min_length=8)):
    if not _safe_url(url):
        raise HTTPException(status_code=400, detail="非法的 URL")

    try:
        cli = await _get_client()
        resp = await cli.get(url)
    except (httpx.PoolTimeout, httpx.ReadError):
        # Pool got stuck — recycle and retry once.
        await aclose_client()
        cli = await _get_client()
        try:
            resp = await cli.get(url)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"圖片抓取失敗: {exc}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"圖片抓取失敗: {exc}") from exc

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="upstream non-200")

    ctype = resp.headers.get("content-type", "image/jpeg")
    if not ctype.startswith("image/"):
        ctype = "image/jpeg"

    return Response(
        content=resp.content,
        media_type=ctype,
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Proxy-Status": str(resp.status_code),
        },
    )
