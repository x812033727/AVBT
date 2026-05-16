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

import random
import re
from typing import Any
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from ..config import settings
from ..schemas import (
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
):
    """Return the highest-quality magnet that hasn't been sent before.

    Preference: subtitle > HD > size > date.
    Filters: hd_only, subtitle_only, skip_hashes, and an optional
    [min_size_mb, max_size_mb] byte-size window. Magnets whose advertised
    size can't be parsed (empty string etc.) are kept regardless of the
    size window so we don't silently drop the only candidate.
    """
    skip_hashes = skip_hashes or set()
    min_b = (min_size_mb or 0) * _MB
    max_b = (max_size_mb or 0) * _MB

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


class JavbusBlocked(RuntimeError):
    """Raised when JavBus refuses to serve content (region-blocked)."""


def _client(**overrides: Any) -> httpx.AsyncClient:
    kwargs: dict[str, Any] = dict(
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
    kwargs.update(overrides)
    return httpx.AsyncClient(**kwargs)


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


async def _fetch(cli: httpx.AsyncClient, url: str, *, referer: str | None = None) -> str:
    headers = {"Referer": referer} if referer else None
    resp = await cli.get(url, headers=headers)
    if resp.status_code == 404:
        return ""
    resp.raise_for_status()
    if _is_age_gate(resp.text):
        ok = await _bypass_age_gate(cli, url)
        if not ok:
            raise JavbusBlocked(
                "JavBus 持續要求年齡驗證 — 此 IP 可能被地區阻擋。"
                "請在 .env 設定 HTTP_PROXY 或改用鏡像站 (JAVBUS_BASE_URL)。"
            )
        resp = await cli.get(url, headers=headers)
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


async def search(keyword: str, page: int = 1, uncensored: bool = False) -> SearchResult:
    """Search by code / keyword."""
    keyword = keyword.strip()
    base = settings.javbus_base_url.rstrip("/")
    prefix = "/uncensored/search" if uncensored else "/search"
    url = f"{base}{prefix}/{keyword}/{max(1, page)}"

    async with _client() as cli:
        html = await _fetch(cli, url)
        if not html:
            return SearchResult(items=[], page=page, has_next=False)

    return _parse_listing(html, page)


async def fetch_listing(
    kind: str, slug: str, page: int = 1, uncensored: bool = False
) -> SearchResult:
    """Generic /{kind}/{slug}[/{page}] listing fetcher.

    Works for star / genre / studio / label / series / director — every
    JavBus listing page uses the same ``a.movie-box`` grid markup.
    """
    if kind not in LISTING_KINDS:
        raise ValueError(f"unknown listing kind: {kind}")
    slug = slug.strip()
    base = settings.javbus_base_url.rstrip("/")
    prefix = f"/uncensored/{kind}" if uncensored else f"/{kind}"
    url = f"{base}{prefix}/{slug}/{max(1, page)}"

    async with _client() as cli:
        html = await _fetch(cli, url)
        if not html:
            return SearchResult(items=[], page=page, has_next=False)

    return _parse_listing(html, page)


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
    """Return the page title (e.g. 'SODクリエイト') for /{kind}/{slug}."""
    if kind not in LISTING_KINDS:
        return ""
    slug = slug.strip()
    base = settings.javbus_base_url.rstrip("/")
    prefix = f"/uncensored/{kind}" if uncensored else f"/{kind}"
    url = f"{base}{prefix}/{slug}/1"
    try:
        async with _client() as cli:
            html = await _fetch(cli, url)
            if not html:
                return ""
        return _parse_listing_title(html)
    except Exception:
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
    async with _client() as cli:
        html = await _fetch(cli, url)
        if not html:
            return None
    return _parse_star_profile(html, star_id)


async def fetch_genre(genre_id: str, page: int = 1, uncensored: bool = False) -> SearchResult:
    return await fetch_listing("genre", genre_id, page=page, uncensored=uncensored)


async def fetch_detail(code: str) -> MovieDetail:
    code = code.strip().upper()
    base = settings.javbus_base_url.rstrip("/")
    url = f"{base}/{code}"

    async with _client() as cli:
        html = await _fetch(cli, url)
        if not html:
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
            )

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
    cli: httpx.AsyncClient, *, referer: str, gid: str, uc: str, img: str
) -> list[Magnet]:
    base = settings.javbus_base_url.rstrip("/")
    url = (
        f"{base}/ajax/uncledatoolsbyajax.php"
        f"?gid={gid}&lang={settings.javbus_lang}&img={img}"
        f"&uc={uc}&floor={random.randint(100, 999)}"
    )
    resp = await cli.get(url, headers={"Referer": referer})
    if resp.status_code != 200 or not resp.text.strip():
        return []
    soup = BeautifulSoup(resp.text, "lxml")
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
            )
        )
    return magnets
