"""JavBus scraper.

Search list parsing + detail page parsing + magnet AJAX fetch.

The site loads the magnet table asynchronously after the detail page, via:
    /ajax/uncledatoolsbyajax.php?gid=...&lang=...&img=...&uc=...&floor=...
The required tokens (gid / uc / img) are embedded as JS variables on the
detail HTML. We parse them with regex, then issue the AJAX call with the
detail URL as Referer (the server enforces that).

Region-based age gate: requests from some IPs (mostly outside Asia) are
redirected to ``/doc/driver-verify``. We auto-POST the verify form once
per request, but the server may still refuse if the IP is geo-blocked —
in that case use ``HTTP_PROXY`` or change ``JAVBUS_BASE_URL`` to a mirror.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Any
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from ..config import settings
from ..services.scraper_health import scraper_health

logger = logging.getLogger(__name__)


class RateLimiter:
    """Adaptive concurrency + spacing limiter for JavBus requests.

    Caps in-flight requests to ``concurrency`` (via a semaphore) AND
    enforces a minimum spacing between request *starts* that grows on
    429 (``signal_429``) and decays toward the base on each successful
    acquire. The combination lets us run multiple requests in parallel
    in the steady state while still backing off automatically when the
    server pushes back.

    Used as an async context manager — acquire on enter, release on
    exit. ``signal_429`` is called from the 429 retry path so the next
    request waits longer."""

    def __init__(
        self, *, concurrency: int, min_interval: float,
        penalty: float, recovery: float,
    ) -> None:
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._base = max(0.0, min_interval)
        self._cur = self._base
        self._penalty = max(1.0, penalty)
        self._recovery = max(0.0, min(1.0, recovery))
        # Hard ceiling so a 429 storm can't push spacing to infinity.
        self._ceiling = max(self._base * 20.0, 30.0)
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> RateLimiter:
        await self._sem.acquire()
        async with self._lock:
            wait = self._last + self._cur - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
            if self._cur > self._base:
                self._cur = max(self._base, self._cur * self._recovery)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._sem.release()

    def signal_429(self) -> None:
        """Widen spacing in response to a 429. Idempotent enough that
        calling it once per failed attempt is fine."""
        new = min(self._ceiling, max(self._cur, self._base) * self._penalty)
        if new > self._cur:
            logger.warning(
                "JavBus rate limiter widened: %.2fs -> %.2fs", self._cur, new
            )
            self._cur = new


_limiter = RateLimiter(
    concurrency=settings.javbus_concurrency,
    min_interval=settings.javbus_min_interval,
    penalty=settings.javbus_429_penalty,
    recovery=settings.javbus_429_recovery,
)


# Shared httpx.AsyncClient. Built once at FastAPI lifespan startup so
# every JavBus request reuses the same connection pool (keep-alive +
# optional HTTP/2). Set on ``init_client``, cleared on ``aclose_client``.
_shared_client: httpx.AsyncClient | None = None


async def init_client() -> None:
    """Build the shared httpx.AsyncClient. Idempotent — calling twice is
    a no-op so reloads don't leak clients."""
    global _shared_client
    if _shared_client is not None:
        return
    want_http2 = bool(settings.javbus_http2)
    if want_http2:
        # The h2 package is an optional httpx dep. Probe before binding
        # so older installs that haven't reinstalled requirements still
        # start up — they just get HTTP/1.1.
        try:
            import h2  # noqa: F401
        except ImportError:
            logger.warning(
                "JAVBUS_HTTP2=true but the 'h2' package isn't installed; "
                "falling back to HTTP/1.1. Run `pip install httpx[http2]` to enable."
            )
            want_http2 = False
    kwargs: dict[str, Any] = dict(
        http2=want_http2,
        limits=httpx.Limits(
            max_connections=settings.javbus_pool_size,
            max_keepalive_connections=settings.javbus_pool_size,
        ),
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        },
        cookies=DEFAULT_COOKIES,
        follow_redirects=True,
        timeout=30.0,
    )
    if settings.http_proxy:
        kwargs["proxy"] = settings.http_proxy
    _shared_client = httpx.AsyncClient(**kwargs)
    logger.info(
        "JavBus shared client ready: http2=%s pool=%d concurrency=%d "
        "min_interval=%.2fs",
        want_http2, settings.javbus_pool_size,
        settings.javbus_concurrency, settings.javbus_min_interval,
    )


async def aclose_client() -> None:
    """Close the shared client. Called from FastAPI lifespan shutdown."""
    global _shared_client
    if _shared_client is None:
        return
    await _shared_client.aclose()
    _shared_client = None


def _get_client() -> httpx.AsyncClient:
    """Accessor — raises if the lifespan startup didn't run, which
    immediately surfaces ordering bugs instead of silently leaking
    short-lived clients."""
    if _shared_client is None:
        raise RuntimeError(
            "javbus scraper not initialised — call init_client() in lifespan startup"
        )
    return _shared_client


from ..schemas import (  # noqa: E402 — after module-level client setup
    ActressRef,
    GenreRef,
    LinkRef,
    Magnet,
    MovieDetail,
    MovieListItem,
    SearchResult,
    StarProfile,
)

# JavBus listing pages all use the same /<kind>/<slug>[/<page>] layout.
LISTING_KINDS = {"star", "genre", "studio", "label", "series", "director"}


def extract_btih(magnet: str) -> str:
    """Return the upper-case btih hash from a magnet URI (or empty)."""
    m = re.search(r"xt=urn:btih:([A-Za-z0-9]+)", magnet)
    return m.group(1).upper() if m else ""


_SIZE_RE = re.compile(r"([\d.]+)\s*([KMGT]?i?B)", re.I)


def parse_size(s: str) -> float:
    """Parse '2.34GB' → bytes. Returns 0 on parse failure."""
    m = _SIZE_RE.search(s or "")
    if not m:
        return 0.0
    n = float(m.group(1))
    unit = m.group(2).upper().replace("I", "")
    mult = {"B": 1.0, "KB": 1024.0, "MB": 1024.0 ** 2,
            "GB": 1024.0 ** 3, "TB": 1024.0 ** 4}
    return n * mult.get(unit, 1.0)


_MB = 1024.0 ** 2


def pick_best_magnet(
    magnets,
    *,
    hd_only: bool = True,
    subtitle_only: bool = False,
    skip_hashes: set[str] | None = None,
    min_size_mb: float | None = None,
    max_size_mb: float | None = None,
    prefer_max_size_mb: float | None = None,
):
    """Return the highest-quality magnet that hasn't been sent before.

    Preference: subtitle > HD > size > date.
    Filters: hd_only, subtitle_only, skip_hashes, and an optional
    [min_size_mb, max_size_mb] byte-size window. Magnets whose advertised
    size can't be parsed (empty string etc.) are kept regardless of the
    size window so we don't silently drop the only candidate.
    prefer_max_size_mb is a soft cap: candidates at or below it are
    preferred, but if every candidate exceeds it we keep the oversized
    ones rather than returning nothing.
    """
    skip_hashes = skip_hashes or set()
    min_b = (min_size_mb or 0) * _MB
    max_b = (max_size_mb or 0) * _MB
    prefer_max_b = (prefer_max_size_mb or 0) * _MB

    def within_size(m) -> bool:
        size = parse_size(m.size)
        if size <= 0:
            return True  # unknown size, don't reject
        if min_b and size < min_b:
            return False
        if max_b and size > max_b:
            return False
        return True

    candidates = list(magnets)
    if hd_only:
        hd = [m for m in candidates if m.is_hd]
        if hd:
            candidates = hd
    if subtitle_only:
        sub = [m for m in candidates if m.has_subtitle]
        if sub:
            candidates = sub
    candidates = [m for m in candidates if extract_btih(m.link) not in skip_hashes]
    candidates = [m for m in candidates if within_size(m)]
    if prefer_max_b:
        under = [
            m for m in candidates
            if parse_size(m.size) <= 0 or parse_size(m.size) <= prefer_max_b
        ]
        if under:
            candidates = under
    if not candidates:
        return None
    candidates.sort(
        key=lambda m: (
            0 if m.has_subtitle else 1,
            0 if m.is_hd else 1,
            -parse_size(m.size),
            -1 * len(m.date),
            m.date,
        )
    )
    return candidates[0]


_STAR_PATH_RE = re.compile(r"/star/([^/?#]+)")
_GENRE_PATH_RE = re.compile(r"/genre/([^/?#]+)")


def _link_ref_from_p(p, kind: str) -> LinkRef | None:
    """Pull a {name, id} ref from the <a href='/{kind}/...'> inside <p>.

    Falls back to the <p>'s plain text (after stripping the header) when
    JavBus renders the value without a link."""
    a = p.select_one(f"a[href*='/{kind}/']")
    if a:
        href = a.get("href", "") or ""
        m = re.search(rf"/{kind}/([^/?#]+)", href)
        name = _text(a)
        if name:
            return LinkRef(name=name, id=m.group(1) if m else "")
    text = p.get_text(" ", strip=True)
    head = _text(p.select_one("span.header"))
    if head:
        text = text.replace(head, "", 1).strip()
    return LinkRef(name=text, id="") if text else None


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Show items that have magnet links by default.
DEFAULT_COOKIES = {"existmag": "mag", "age": "verified"}

GID_RE = re.compile(r"var\s+gid\s*=\s*(\d+)")
UC_RE = re.compile(r"var\s+uc\s*=\s*(\d+)")
IMG_RE = re.compile(r"var\s+img\s*=\s*['\"]([^'\"]+)['\"]")

AGE_GATE_MARKERS = ("driver-verify", "Age Verification")

# Cloudflare (and lookalike) anti-bot interstitials come back as
# HTTP 200 with a challenge document — without detection they parse as
# a valid-but-empty page and the failure is invisible.
CHALLENGE_MARKERS = (
    "Just a moment",
    "cf-browser-verification",
    "challenge-platform",
    "__cf_chl_",
    "Attention Required",
)


def _looks_like_challenge(html: str) -> bool:
    return any(marker in html for marker in CHALLENGE_MARKERS)


class JavbusBlocked(RuntimeError):
    """Raised when JavBus refuses to serve content (region-blocked)."""


def _is_age_gate(html: str) -> bool:
    return any(marker in html for marker in AGE_GATE_MARKERS)


async def _bypass_age_gate(cli: httpx.AsyncClient, target_url: str) -> bool:
    """POST the age-verification form. Returns True on success."""
    base = settings.javbus_base_url.rstrip("/")
    verify_url = f"{base}/doc/driver-verify?referer={quote(target_url, safe='')}"
    try:
        await cli.get(verify_url)
        resp = await cli.post(
            verify_url,
            data={"Submit": "確認"},
            headers={"Referer": verify_url},
        )
        return not _is_age_gate(resp.text)
    except httpx.HTTPError:
        return False


def _cookie_header_with(cli: httpx.AsyncClient, overrides: dict[str, str]) -> str:
    """Build an explicit ``Cookie`` request header from the client jar
    with ``overrides`` applied on top.

    We deliberately do NOT pass ``cookies=overrides`` to the request.
    httpx merges request cookies into the jar keyed by (name, domain,
    path), so when the jar already holds the same name carrying a domain
    — and JavBus's own ``Set-Cookie`` gives ``existmag`` one — the
    override is emitted as a *second* ``existmag=…`` rather than
    replacing the first. PHP then reads the last duplicate and silently
    ignores our override, so an ``existmag=all`` full-catalog walk gets
    served the magnet-only listing. Magnet-less works the user owns then
    look like they're absent from the catalog and get false-flagged as
    多餘 (extras).

    An explicit ``Cookie`` header is sent verbatim — cookielib's
    ``add_cookie_header`` skips the jar entirely when the request already
    carries one — so exactly one value per name reaches the server while
    every other jar cookie (age gate / session) is preserved.
    """
    pairs: dict[str, str] = {}
    for c in cli.cookies.jar:
        if c.name in overrides:
            continue
        pairs[c.name] = c.value or ""
    pairs.update(overrides)
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


async def _fetch(
    cli: httpx.AsyncClient,
    url: str,
    *,
    referer: str | None = None,
    cookies: dict[str, str] | None = None,
) -> str:
    def build_headers() -> dict[str, str] | None:
        # Rebuilt per attempt so a cookie the server set on an earlier
        # response (429 Set-Cookie, age-gate bypass) is reflected in the
        # explicit header.
        h: dict[str, str] = {}
        if referer:
            h["Referer"] = referer
        if cookies:
            h["Cookie"] = _cookie_header_with(cli, cookies)
        return h or None

    # Up to 4 attempts on 429. Backoff doubles: 4s, 8s, 16s. The rate
    # limiter is entered once per attempt so each retry respects both
    # the configured spacing AND the dynamic widening that signal_429
    # applies after a server pushback.
    resp = None
    for attempt in range(4):
        async with _limiter:
            resp = await cli.get(url, headers=build_headers())
        if resp.status_code == 404:
            return ""
        if resp.status_code == 429:
            _limiter.signal_429()
            if attempt == 3:
                # Give up — let it propagate so the caller can surface
                # the error to the user (it's already a clear message).
                resp.raise_for_status()
            wait = 4.0 * (2 ** attempt) + random.uniform(0, 1.5)
            logger.warning(
                "JavBus 429 on %s (attempt %d) — backing off %.1fs",
                url, attempt + 1, wait,
            )
            await asyncio.sleep(wait)
            continue
        if resp.status_code >= 500:
            # Transient origin/CDN errors: 502/503/504 and the Cloudflare
            # 52x family (522 = couldn't reach JavBus origin). Unlike 429
            # these aren't rate-limit pushback, so we DON'T widen the
            # limiter — just back off and retry; most clear within a few
            # seconds. On the last attempt let it propagate so the failure
            # surfaces (the walker turns it into an errored listing rather
            # than a silently-empty catalog).
            if attempt == 3:
                resp.raise_for_status()
            wait = 2.0 * (2 ** attempt) + random.uniform(0, 1.0)
            logger.warning(
                "JavBus %d on %s (attempt %d) — backing off %.1fs",
                resp.status_code, url, attempt + 1, wait,
            )
            await asyncio.sleep(wait)
            continue
        resp.raise_for_status()
        break

    assert resp is not None
    if _looks_like_challenge(resp.text):
        scraper_health.record_challenge()
        logger.warning("JavBus anti-bot challenge page on %s", url)
        raise JavbusBlocked(
            "JavBus 回應反機器人挑戰頁(Cloudflare)——此 IP 可能被盯上。"
            "請稍後再試,持續發生時考慮設 HTTP_PROXY 或改用鏡像站。"
        )
    if _is_age_gate(resp.text):
        ok = await _bypass_age_gate(cli, url)
        if not ok:
            raise JavbusBlocked(
                "JavBus 持續要求年齡驗證 — 此 IP 可能被地區阻擋。"
                "請在 .env 設定 HTTP_PROXY 或改用鏡像站 (JAVBUS_BASE_URL)。"
            )
        async with _limiter:
            resp = await cli.get(url, headers=build_headers())
        resp.raise_for_status()
    return resp.text


def _abs(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http"):
        return url
    return urljoin(settings.javbus_base_url.rstrip("/") + "/", url.lstrip("/"))


def _text(node) -> str:
    return node.get_text(strip=True) if node else ""


_NEXT_TEXT_RE = re.compile(r"下一頁|next|»|>$", re.I)


def _parse_listing(html: str, page: int) -> SearchResult:
    soup = BeautifulSoup(html, "lxml")
    items: list[MovieListItem] = []
    for a in soup.select("a.movie-box"):
        href = _abs(a.get("href", ""))
        img_node = a.select_one("img")
        cover = _abs(img_node.get("src", "")) if img_node else ""
        title = img_node.get("title", "").strip() if img_node else ""
        dates = a.select("date")
        code = _text(dates[0]) if dates else ""
        date = _text(dates[1]) if len(dates) > 1 else ""
        if not code:
            code = href.rsplit("/", 1)[-1]
        items.append(
            MovieListItem(code=code, title=title, cover=cover, detail_url=href, date=date)
        )

    # JavBus paginates in a couple of slightly different shapes depending
    # on which listing page you're on (search vs. star vs. series etc.).
    # We try the canonical ul.pagination first, then fall back to any
    # anchor whose text or rel attribute looks like a "next" link.
    has_next = False
    total_pages: int | None = None

    pagination = soup.select_one("ul.pagination") or soup.select_one(
        "ul.pagination-clean, nav .pagination"
    )
    if pagination:
        page_links = pagination.select("li a, a")
        nums = [int(_text(pl)) for pl in page_links if _text(pl).isdigit()]
        if nums:
            total_pages = max(nums)
            has_next = page < total_pages
        if not has_next:
            for pl in page_links:
                txt = _text(pl)
                rel = " ".join(pl.get("rel") or [])
                if _NEXT_TEXT_RE.search(txt) or "next" in rel.lower():
                    has_next = True
                    break

    if not has_next:
        # Last-resort scan: any <a> below the grid whose text looks like
        # a "next" indicator (and whose href isn't a JS no-op).
        for a in soup.find_all("a"):
            txt = _text(a)
            if not txt or not _NEXT_TEXT_RE.search(txt):
                continue
            href = (a.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:")):
                continue
            has_next = True
            break

    return SearchResult(items=items, page=page, has_next=has_next, total_pages=total_pages)


async def search(
    keyword: str,
    page: int = 1,
    uncensored: bool = False,
    *,
    with_magnets_only: bool = True,
) -> SearchResult:
    """Search by code / keyword.

    ``with_magnets_only`` mirrors :func:`fetch_listing`: the default
    ``existmag=mag`` filter only returns works that still have magnets.
    Pass ``False`` to walk the full catalog (``existmag=all``) — used when
    resolving a code's canonical id, where a work whose magnets have aged
    off JavBus must still surface so its detail-page id can be read."""
    keyword = keyword.strip()
    base = settings.javbus_base_url.rstrip("/")
    prefix = "/uncensored/search" if uncensored else "/search"
    url = f"{base}{prefix}/{keyword}/{max(1, page)}"

    cookies = None if with_magnets_only else {"existmag": "all"}
    html = await _fetch_listing_html(url, cookies=cookies)
    if not html:
        return SearchResult(items=[], page=page, has_next=False)

    return _parse_listing_recorded(html, page)


async def fetch_listing(
    kind: str,
    slug: str,
    page: int = 1,
    uncensored: bool = False,
    *,
    with_magnets_only: bool = True,
) -> SearchResult:
    """Generic /{kind}/{slug}[/{page}] listing fetcher.

    Works for star / genre / studio / label / series / director — every
    JavBus listing page uses the same ``a.movie-box`` grid markup.

    ``with_magnets_only`` controls whether the JavBus ``existmag=mag``
    cookie filter is applied to this request. Default ``True`` matches
    the historical behaviour: only works that currently have at least
    one magnet link are returned, which is what the tracker / send-all
    flows want (no point listing un-downloadable codes). Pass ``False``
    for full-catalog walks (e.g. "extras" detection) so works whose
    magnet links have aged off JavBus still appear in the listing —
    otherwise the user has the file in PikPak but the scraper says the
    code isn't in the listing, producing a false-positive "extra".
    """
    if kind not in LISTING_KINDS:
        raise ValueError(f"unknown listing kind: {kind}")
    slug = slug.strip()
    base = settings.javbus_base_url.rstrip("/")
    prefix = f"/uncensored/{kind}" if uncensored else f"/{kind}"
    url = f"{base}{prefix}/{slug}/{max(1, page)}"

    # ``existmag=all`` shows the full catalog; the shared client's
    # default ``existmag=mag`` filters to works that still have magnets.
    # When overriding, ``_fetch`` sends an explicit Cookie header (see
    # ``_cookie_header_with``) so JavBus can't be served a duplicate
    # ``existmag`` and silently keep the magnet-only filter.
    cookies = None if with_magnets_only else {"existmag": "all"}
    html = await _fetch_listing_html(url, cookies=cookies)
    if not html:
        return SearchResult(items=[], page=page, has_next=False)

    return _parse_listing_recorded(html, page)


async def _fetch_listing_html(url: str, *, cookies: dict[str, str] | None) -> str:
    try:
        return await _fetch(_get_client(), url, cookies=cookies)
    except Exception:
        scraper_health.record_listing("error")
        raise


def _parse_listing_recorded(html: str, page: int) -> SearchResult:
    res = _parse_listing(html, page)
    scraper_health.record_listing("ok" if res.items else "zero_items")
    return res


def _parse_listing_title(html: str) -> str:
    """Best-effort: pull the human-readable title from a listing page.

    JavBus puts the actress / studio / series name in the breadcrumb
    h3 inside ``div.container``. Fall back to <title>.
    """
    soup = BeautifulSoup(html, "lxml")
    h3 = soup.select_one("div.container h3")
    text = _text(h3)
    if not text:
        title = soup.select_one("title")
        text = _text(title)
        # Strip the trailing " - JavBus" / " | JavBus" common suffix.
        for sep in (" - JavBus", " | JavBus", " – JavBus"):
            if sep in text:
                text = text.split(sep)[0]
                break
    return text.strip()


async def fetch_listing_title(kind: str, slug: str, uncensored: bool = False) -> str:
    """Return the page title (e.g. 'SODクリエイト') for /{kind}/{slug}.
    Strips JavBus' '- <kind> - 影片' template suffix so the result is
    suitable for use as a folder name."""
    if kind not in LISTING_KINDS:
        return ""
    slug = slug.strip()
    base = settings.javbus_base_url.rstrip("/")
    prefix = f"/uncensored/{kind}" if uncensored else f"/{kind}"
    url = f"{base}{prefix}/{slug}/1"
    try:
        html = await _fetch(_get_client(), url)
        if not html:
            return ""
        from ..services.jav_code import clean_listing_name  # avoid cycle
        return clean_listing_name(_parse_listing_title(html))
    except Exception:  # noqa: BLE001 — listing title is best-effort
        return ""


async def fetch_star(star_id: str, page: int = 1, uncensored: bool = False) -> SearchResult:
    return await fetch_listing("star", star_id, page=page, uncensored=uncensored)


_PROFILE_KEYS = {
    "生日": "birthday",
    "年齡": "age",
    "身高": "height",
    "罩杯": "cup",
    "胸圍": "bust",
    "腰圍": "waist",
    "臀圍": "hip",
    "出生地": "birthplace",
    "愛好": "hobby",
    "Birthday": "birthday",
    "Age": "age",
    "Height": "height",
    "Cup": "cup",
    "Bust": "bust",
    "Waist": "waist",
    "Hip": "hip",
}


def _parse_star_profile(html: str, star_id: str) -> StarProfile | None:
    """Parse the actress info box on /star/{id}.

    JavBus markup shifts a bit between mirrors, so we look for the
    avatar-box image (name + photo) and any <p> whose first inline
    element matches one of the known headers."""
    soup = BeautifulSoup(html, "lxml")
    profile = StarProfile(id=star_id)

    avatar = soup.select_one("div.avatar-box img") or soup.select_one("img.avatar")
    if avatar:
        profile.avatar = _abs(avatar.get("src", ""))
        profile.name = avatar.get("title", "").strip() or profile.name

    name_node = soup.select_one("div.avatar-box span") or soup.select_one(
        "div.photo-info span"
    )
    if name_node and not profile.name:
        profile.name = _text(name_node)

    info_root = soup.select_one("div.avatar-box ~ div.star-info") or soup.select_one(
        "div.star-info"
    )
    fields: dict[str, str] = {}
    if info_root:
        for p in info_root.select("p"):
            raw = p.get_text(":", strip=True)
            if not raw or ":" not in raw:
                continue
            head, _, value = raw.partition(":")
            head = head.strip().rstrip(":").strip()
            value = value.strip()
            key = _PROFILE_KEYS.get(head)
            if key:
                fields[key] = value

    for k, v in fields.items():
        setattr(profile, k, v)

    has_any = profile.avatar or profile.name or any(fields.values())
    return profile if has_any else None


async def fetch_star_profile(star_id: str, *, uncensored: bool = False) -> StarProfile | None:
    star_id = star_id.strip()
    base = settings.javbus_base_url.rstrip("/")
    prefix = "/uncensored/star" if uncensored else "/star"
    url = f"{base}{prefix}/{star_id}/1"
    html = await _fetch(_get_client(), url)
    if not html:
        return None
    return _parse_star_profile(html, star_id)


async def fetch_genre(genre_id: str, page: int = 1, uncensored: bool = False) -> SearchResult:
    return await fetch_listing("genre", genre_id, page=page, uncensored=uncensored)


# fetch_detail in-memory cache: {code: (stored_at_monotonic, MovieDetail)}.
# Same code requested from tracker / bulk / movie page collapses to one
# fetch within TTL. Cache is per-process — restart drops it.
_detail_cache: dict[str, tuple[float, MovieDetail]] = {}
_detail_cache_lock = asyncio.Lock()
# In-flight dedup: when two callers race the same code, the second one
# awaits the first's Event instead of issuing a duplicate HTTP pair.
_detail_inflight: dict[str, asyncio.Event] = {}


def _detail_cache_get(code: str) -> MovieDetail | None:
    ttl = settings.javbus_detail_cache_ttl_seconds
    if ttl <= 0:
        return None
    entry = _detail_cache.get(code)
    if entry is None:
        return None
    stored_at, detail = entry
    if time.monotonic() - stored_at > ttl:
        _detail_cache.pop(code, None)
        return None
    return detail


def _detail_cache_put(code: str, detail: MovieDetail) -> None:
    if settings.javbus_detail_cache_ttl_seconds <= 0:
        return
    _detail_cache[code] = (time.monotonic(), detail)
    cap = max(1, settings.javbus_detail_cache_max)
    if len(_detail_cache) > cap:
        # Trim oldest 25% by stored timestamp. Cheap O(n log n), runs
        # only when over capacity.
        oldest = sorted(_detail_cache.items(), key=lambda kv: kv[1][0])
        drop = max(1, cap // 4)
        for k, _ in oldest[:drop]:
            _detail_cache.pop(k, None)


async def fetch_detail(code: str, *, refresh: bool = False) -> MovieDetail:
    code = code.strip().upper()
    owns_inflight = False
    if not refresh:
        cached = _detail_cache_get(code)
        if cached is not None:
            return cached
        # Coalesce concurrent callers onto the same in-flight fetch.
        async with _detail_cache_lock:
            cached = _detail_cache_get(code)
            if cached is not None:
                return cached
            event = _detail_inflight.get(code)
            if event is None:
                event = asyncio.Event()
                _detail_inflight[code] = event
                owns_inflight = True
        if not owns_inflight:
            await event.wait()
            cached = _detail_cache_get(code)
            if cached is not None:
                return cached
            # Owner finished but didn't cache (empty title / error). Fall
            # through to fetch ourselves — rare and we deliberately do
            # NOT register a new in-flight event here to avoid races.

    base = settings.javbus_base_url.rstrip("/")
    url = f"{base}/{code}"
    cli = _get_client()

    try:
        html = await _fetch(cli, url)
        if not html:
            scraper_health.record_detail("empty_html")
            return MovieDetail(code=code, title="")

        detail = _parse_detail(html, code)

        # Extract AJAX tokens for the magnet table.
        gid_m = GID_RE.search(html)
        uc_m = UC_RE.search(html)
        img_m = IMG_RE.search(html)
        if gid_m and uc_m and img_m:
            detail.magnets = await _fetch_magnets(
                cli,
                referer=url,
                gid=gid_m.group(1),
                uc=uc_m.group(1),
                img=img_m.group(1),
                code=code,
            )
            # Empty AJAX result = fresh release without magnets yet;
            # only a missing token on a parsed page signals breakage.
            scraper_health.record_detail(
                "ok_magnets" if detail.magnets else "ok_no_magnets"
            )
        elif detail.title:
            scraper_health.record_detail("gid_missing")
        else:
            scraper_health.record_detail("empty_parse")

        if detail.title:
            _detail_cache_put(code, detail)
        return detail
    except Exception:
        scraper_health.record_detail("error")
        raise
    finally:
        if owns_inflight:
            async with _detail_cache_lock:
                pending = _detail_inflight.pop(code, None)
            if pending is not None:
                pending.set()


# Codes whose detail page is empty AND whose search lookup found no
# matching canonical id. Cached (with a TTL) so the periodic sweep /
# organize / archive passes don't re-search the same unresolvable name
# every cycle. Only *stable* misses are recorded — a search that errored
# out (network / 429) is treated as transient and retried next time.
_unresolved_cache: dict[str, float] = {}
_UNRESOLVED_TTL = 1800.0


def _norm_code(code: str) -> str:
    # Local import mirrors the existing ``clean_listing_name`` pattern —
    # jav_code is stdlib-only at import time, so this can't cycle.
    from ..services.jav_code import normalize_code

    return normalize_code(code) or (code or "").strip().upper()


def _code_from_url(url: str) -> str:
    """Last path segment of a JavBus detail URL (the real page id, which
    keeps the numeric prefix even when the listing shows it stripped)."""
    if not url:
        return ""
    tail = url.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return tail.strip()


async def _search_canonical_code(code: str) -> str | None:
    """Find JavBus's canonical identifier for a code whose detail page
    doesn't serve directly.

    Some amateur lines are indexed only under a numeric-prefixed id
    (``259LUXU-1543``); a prefix-stripped code (``LUXU-1543``) 404s on the
    detail page but still surfaces via search. Returns the first search
    hit whose normalized code equals ``code`` — so we never grab an
    unrelated work — preferring the hit's detail-page id (which keeps the
    numeric prefix). Returns ``None`` on no match."""
    want = _norm_code(code)
    if not want:
        return None
    now = time.monotonic()
    ts = _unresolved_cache.get(want)
    if ts is not None and now - ts < _UNRESOLVED_TTL:
        return None
    try:
        # Full catalog: a held work whose magnets aged off JavBus must
        # still surface so we can read its (prefixed) detail-page id.
        result = await search(code, with_magnets_only=False)
    except Exception as exc:  # noqa: BLE001
        # Transient (network / geo-block / 429) — don't poison the cache.
        logger.debug("canonical search for %s failed: %s", code, exc)
        return None
    for it in result.items:
        cand = _code_from_url(it.detail_url) or it.code
        if cand and (_norm_code(cand) == want or _norm_code(it.code) == want):
            return cand
    # Stable miss: JavBus answered but has nothing matching. Remember so
    # repeat passes skip the search. Prune opportunistically.
    if len(_unresolved_cache) > 1000:
        cutoff = now - _UNRESOLVED_TTL
        for k in [k for k, v in _unresolved_cache.items() if v < cutoff]:
            _unresolved_cache.pop(k, None)
    _unresolved_cache[want] = now
    return None


async def fetch_detail_resolved(code: str, *, refresh: bool = False) -> MovieDetail:
    """:func:`fetch_detail` with an amateur-label numeric-prefix fallback.

    JavBus indexes some amateur lines (``259LUXU``, ``200GANA``,
    ``300MIUM`` …) only under their numeric-prefixed id.
    ``extract_jav_code`` deliberately strips that prefix so the presence
    index matches the listing, but the stripped code (``LUXU-1543``) then
    can't reach the detail page (``/259LUXU-1543``). When the direct fetch
    comes back empty, search JavBus for the canonical id and fetch that
    instead, caching the result under the queried code so repeat lookups
    (the archiver re-resolves on every pass) skip the search.

    Callers that resolve a code to its listing membership — the archiver,
    the reorganize sweep, pCloud organize, the missing detail-probe and
    the download queue — use this so these labels get archived under
    their series instead of stranded in the fallback bucket. Falls back to
    the (empty) direct result when no canonical id matches, so a genuinely
    unknown code behaves exactly as before."""
    detail = await fetch_detail(code, refresh=refresh)
    if detail.title:
        return detail
    real = await _search_canonical_code(code)
    if not real or real.strip().upper() == (code or "").strip().upper():
        return detail
    alt = await fetch_detail(real, refresh=refresh)
    if alt.title:
        # Cache under the queried code too so the next resolve of the same
        # stripped code is a straight cache hit, not another search.
        _detail_cache_put((code or "").strip().upper(), alt)
        return alt
    return detail


def _parse_detail(html: str, code: str) -> MovieDetail:
    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one("div.container")
    h3 = container.select_one("h3") if container else None
    title = _text(h3)
    if title.upper().startswith(code):
        title = title[len(code):].strip()

    big = soup.select_one("a.bigImage img") or soup.select_one("a.bigImage")
    cover = ""
    if big:
        cover = _abs(big.get("src") or big.get("href") or "")

    info_root = soup.select_one("div.info")
    release_date = duration = ""
    studio: LinkRef | None = None
    label: LinkRef | None = None
    director: LinkRef | None = None
    series: LinkRef | None = None
    actresses: list[ActressRef] = []
    genres: list[GenreRef] = []

    if info_root:
        for p in info_root.select("p"):
            label_node = p.select_one("span.header")
            head = _text(label_node)
            value = p.get_text(" ", strip=True)
            if label_node:
                value = value.replace(head, "", 1).strip()
            if "識別碼" in head or "ID" in head.upper():
                pass
            elif "發行日期" in head or "Release Date" in head:
                release_date = value
            elif "長度" in head or "Length" in head:
                duration = value
            elif "導演" in head or "Director" in head:
                director = _link_ref_from_p(p, "director")
            elif "製作商" in head or "Studio" in head:
                studio = _link_ref_from_p(p, "studio")
            elif "發行商" in head or "Label" in head:
                label = _link_ref_from_p(p, "label")
            elif "系列" in head or "Series" in head:
                series = _link_ref_from_p(p, "series")
            elif "類別" in head or "Genre" in head:
                for a in p.select("a"):
                    name = _text(a)
                    if not name:
                        continue
                    href = a.get("href", "")
                    gm = _GENRE_PATH_RE.search(href)
                    genres.append(GenreRef(name=name, id=gm.group(1) if gm else ""))

        # Actresses live in their own block below the genre paragraphs.
        star_block = info_root.find("p", class_="star-show")
        if star_block:
            sibling = star_block.find_next_sibling("p")
            if sibling:
                for a in sibling.select("a"):
                    name = _text(a)
                    if not name:
                        # Some markup wraps the name in a <span> inside the <a>
                        span = a.select_one("span")
                        name = _text(span) if span else ""
                    if not name:
                        continue
                    href = a.get("href", "")
                    sm = _STAR_PATH_RE.search(href)
                    actresses.append(ActressRef(name=name, id=sm.group(1) if sm else ""))

    samples = []
    for a in soup.select("a.sample-box"):
        href = a.get("href", "")
        if href:
            samples.append(_abs(href))

    return MovieDetail(
        code=code,
        title=title,
        cover=cover,
        release_date=release_date,
        duration=duration,
        studio=studio,
        label=label,
        director=director,
        series=series,
        actresses=actresses,
        genres=genres,
        samples=samples,
        magnets=[],
    )


async def _fetch_magnets(
    cli: httpx.AsyncClient, *, referer: str, gid: str, uc: str, img: str, code: str = ""
) -> list[Magnet]:
    from ..services.jav_code import detect_part_hint  # local: avoid cycle

    base = settings.javbus_base_url.rstrip("/")
    url = (
        f"{base}/ajax/uncledatoolsbyajax.php"
        f"?gid={gid}&lang={settings.javbus_lang}&img={img}"
        f"&uc={uc}&floor={random.randint(100, 999)}"
    )
    # Route through _fetch so the AJAX call also gets rate limiting +
    # 429 retry. _fetch returns "" on 404 — magnet table sometimes
    # 404s for very old codes; treat as "no magnets".
    try:
        body = await _fetch(cli, url, referer=referer)
    except httpx.HTTPError as exc:
        logger.debug("magnet AJAX %s failed: %s", url, exc)
        return []
    if not body.strip():
        return []
    soup = BeautifulSoup(body, "lxml")
    magnets: list[Magnet] = []
    for tr in soup.select("tr"):
        link_a = tr.select_one("a[href^='magnet:']")
        if not link_a:
            continue
        link = link_a.get("href", "")
        name = _text(link_a)
        tds = tr.find_all("td")
        size = _text(tds[1].select_one("a")) if len(tds) > 1 else ""
        date = _text(tds[2].select_one("a")) if len(tds) > 2 else ""
        is_hd = bool(tr.find(string=re.compile(r"高清|HD", re.I)))
        has_subtitle = bool(tr.find(string=re.compile(r"字幕|subtitle", re.I)))
        magnets.append(
            Magnet(
                name=name,
                link=link,
                size=size,
                date=date,
                is_hd=is_hd,
                has_subtitle=has_subtitle,
                part_hint=detect_part_hint(name, code),
            )
        )
    return magnets
