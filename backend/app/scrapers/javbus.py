"""JavBus scraper.

Search list parsing + detail page parsing + magnet AJAX fetch.

The site loads the magnet table asynchronously after the detail page, via:
    /ajax/uncledatoolsbyajax.php?gid=...&lang=...&img=...&uc=...&floor=...
The required tokens (gid / uc / img) are embedded as JS variables on the
detail HTML. We parse them with regex, then issue the AJAX call with the
detail URL as Referer (the server enforces that).
"""

from __future__ import annotations

import random
import re
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from ..config import settings
from ..schemas import Magnet, MovieDetail, MovieListItem, SearchResult


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Show items that have magnet links by default.
DEFAULT_COOKIES = {"existmag": "mag", "age": "verified"}

GID_RE = re.compile(r"var\s+gid\s*=\s*(\d+)")
UC_RE = re.compile(r"var\s+uc\s*=\s*(\d+)")
IMG_RE = re.compile(r"var\s+img\s*=\s*['\"]([^'\"]+)['\"]")


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


async def search(keyword: str, page: int = 1, uncensored: bool = False) -> SearchResult:
    """Search by code / keyword. Returns list of MovieListItem.

    JavBus paths:
        /search/{keyword}/{page}
        /uncensored/search/{keyword}/{page}
    """
    keyword = keyword.strip()
    base = settings.javbus_base_url.rstrip("/")
    prefix = "/uncensored/search" if uncensored else "/search"
    url = f"{base}{prefix}/{keyword}/{max(1, page)}"

    async with _client() as cli:
        resp = await cli.get(url)
        # JavBus returns 404 when the keyword matches nothing.
        if resp.status_code == 404:
            return SearchResult(items=[], page=page, has_next=False)
        resp.raise_for_status()
        html = resp.text

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
            # fallback: try grabbing code from URL tail
            code = href.rsplit("/", 1)[-1]
        items.append(
            MovieListItem(code=code, title=title, cover=cover, detail_url=href, date=date)
        )

    pagination = soup.select_one("ul.pagination")
    has_next = False
    total_pages: int | None = None
    if pagination:
        page_links = pagination.select("li a")
        nums = []
        for pl in page_links:
            t = _text(pl)
            if t.isdigit():
                nums.append(int(t))
        if nums:
            total_pages = max(nums)
            has_next = page < total_pages
        has_next = has_next or any(_text(pl).startswith("下一頁") for pl in page_links)

    return SearchResult(items=items, page=page, has_next=has_next, total_pages=total_pages)


async def fetch_detail(code: str) -> MovieDetail:
    code = code.strip().upper()
    base = settings.javbus_base_url.rstrip("/")
    url = f"{base}/{code}"

    async with _client() as cli:
        resp = await cli.get(url)
        resp.raise_for_status()
        html = resp.text

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
    release_date = duration = studio = label = director = series = ""
    actresses: list[str] = []
    genres: list[str] = []

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
                director = value
            elif "製作商" in head or "Studio" in head:
                studio = value
            elif "發行商" in head or "Label" in head:
                label = value
            elif "系列" in head or "Series" in head:
                series = value
            elif "類別" in head or "Genre" in head:
                genres = [
                    _text(a) for a in p.select("a") if _text(a)
                ]

        # Actresses live in their own block below the genre paragraphs.
        star_block = info_root.find("p", class_="star-show")
        if star_block:
            sibling = star_block.find_next_sibling("p")
            if sibling:
                actresses = [_text(a) for a in sibling.select("a") if _text(a)]

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
