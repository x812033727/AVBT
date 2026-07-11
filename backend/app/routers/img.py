"""Image proxy.

The JavBus cover/avatar CDN refuses requests from the browser due to
hot-link protection and (in our test env) age-gate redirects. We proxy
every image through this endpoint so the browser sees a same-origin
request and we set the Referer the upstream expects.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

from ..config import img_proxy_allowed_hosts, settings
from ..scrapers.javbus import DEFAULT_COOKIES, USER_AGENT
from ..services import img_cache

router = APIRouter(prefix="/api/img", tags=["img"])


_client_singleton: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


def _host_allowed(host: str) -> bool:
    """Suffix entries (leading dot) match the host and any subdomain;
    bare entries match exactly."""
    host = host.lower().rstrip(".")
    for entry in img_proxy_allowed_hosts():
        if entry.startswith("."):
            if host == entry[1:] or host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _safe_url(url: str) -> bool:
    """This proxy is unauthenticated (<img src> can't carry a token), so
    it must not become an open proxy or an SSRF hop: only http(s) URLs on
    allowlisted image hosts pass, and — defense in depth — every address
    the host resolves to must be public. Resolution failure = reject."""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False
        host = p.hostname or ""
        if not host:
            return False
        try:
            ipaddress.ip_address(host)
            # Literal IPs can't match the domain allowlist — reject.
            return False
        except ValueError:
            pass
        if not _host_allowed(host):
            return False
        # When an egress proxy is configured, the proxy resolves the
        # hostname itself and our local DNS answer is meaningless.
        if settings.http_proxy:
            return True
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, p.port or 0, type=socket.SOCK_STREAM)
        addrs = {info[4][0] for info in infos}
        if not addrs:
            return False
        return not any(_ip_blocked(ipaddress.ip_address(a)) for a in addrs)
    except Exception:  # noqa: BLE001 — any parse/resolve failure means reject
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
        # Redirects are followed manually in proxy_image so each hop is
        # re-validated — auto-follow would let an allowlisted host bounce
        # us to an internal address.
        follow_redirects=False,
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


async def _fetch(url: str) -> httpx.Response:
    try:
        cli = await _get_client()
        return await cli.get(url)
    except (httpx.PoolTimeout, httpx.ReadError):
        # Pool got stuck — recycle and retry once.
        await aclose_client()
        cli = await _get_client()
        try:
            return await cli.get(url)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"圖片抓取失敗: {exc}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"圖片抓取失敗: {exc}") from exc


_REDIRECT_CODES = {301, 302, 303, 307, 308}


@router.get("/proxy")
async def proxy_image(url: str = Query(..., min_length=8)):
    if not await _safe_url(url):
        raise HTTPException(status_code=400, detail="非法的 URL")

    # Disk cache is keyed on the requested URL (pre-redirect). Cache
    # lookup happens only after the SSRF check above.
    original_url = url
    hit = await img_cache.lookup(original_url)
    if hit is not None:
        path, media_type = hit
        return FileResponse(
            path,
            media_type=media_type,
            headers={
                "Cache-Control": "public, max-age=86400",
                "X-Img-Cache": "hit",
            },
        )

    resp = await _fetch(url)
    hops = 0
    while resp.status_code in _REDIRECT_CODES and hops < 3:
        location = resp.headers.get("location", "")
        if not location:
            break
        url = urljoin(url, location)
        if not await _safe_url(url):
            raise HTTPException(status_code=400, detail="非法的轉址目標")
        resp = await _fetch(url)
        hops += 1

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="upstream non-200")

    ctype = resp.headers.get("content-type", "image/jpeg")
    if ctype.startswith("image/"):
        # Only genuine image responses are cached; store() skips types
        # it can't encode as an extension.
        await img_cache.store(original_url, resp.content, ctype)
    else:
        ctype = "image/jpeg"

    return Response(
        content=resp.content,
        media_type=ctype,
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Proxy-Status": str(resp.status_code),
            "X-Img-Cache": "miss",
        },
    )
