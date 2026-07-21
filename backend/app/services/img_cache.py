"""On-disk LRU cache for the image proxy.

Files live under ``settings.img_cache_dir`` (inside the compose data
volume) as ``<sha256(url)><ext>`` — the extension encodes the content
type, so a lookup needs no sidecar metadata. Reads bump mtime, which is
the LRU clock; when the directory grows past ``img_cache_max_gb`` the
oldest files are deleted until usage drops to 90% of the cap. All
operations swallow their own errors: a cache failure must degrade to a
live proxy fetch, never to a broken image.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import tempfile
import time
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)

_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/gif": ".gif",
}
_TYPE_BY_EXT = {v: k for k, v in _EXT_BY_TYPE.items()}

_evict_lock = asyncio.Lock()
_last_evict = 0.0  # time.monotonic() of the last eviction scan


def _cache_dir() -> Path:
    return Path(settings.img_cache_dir)


def _key(url: str) -> str:
    # Keyed on the URL as requested (pre-redirect) so redirect chains
    # don't fragment the cache.
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _media_type(ctype: str) -> str:
    return ctype.split(";", 1)[0].strip().lower()


def _lookup_sync(url: str) -> tuple[Path, str] | None:
    base = _cache_dir() / _key(url)
    for ext, mtype in _TYPE_BY_EXT.items():
        path = base.with_name(base.name + ext)
        if path.is_file():
            os.utime(path)  # LRU clock
            return path, mtype
    return None


async def lookup(url: str) -> tuple[Path, str] | None:
    if not settings.img_cache_enabled:
        return None
    try:
        return await asyncio.to_thread(_lookup_sync, url)
    except Exception as exc:  # noqa: BLE001 — cache failure = cache miss
        logger.warning("img cache lookup failed: %s", exc)
        return None


def _store_sync(url: str, content: bytes, ext: str) -> None:
    directory = _cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{_key(url)}{ext}"
    # Per-writer temp file: concurrent stores of the same URL must not share
    # a temp path, or the loser's os.replace hits ENOENT after the winner's
    # rename (and a rename can even publish a half-written file).
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        os.replace(tmp, final)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


async def store(url: str, content: bytes, content_type: str) -> None:
    if not settings.img_cache_enabled or not content:
        return
    ext = _EXT_BY_TYPE.get(_media_type(content_type))
    if ext is None:
        return  # unknown image type — serve live, don't mislabel on disk
    try:
        await asyncio.to_thread(_store_sync, url, content, ext)
    except Exception as exc:  # noqa: BLE001 — cache failure must not break the proxy
        logger.warning("img cache store failed: %s", exc)
        return
    await evict_if_needed()


def _evict_sync() -> tuple[int, int]:
    """Delete oldest-mtime files until usage ≤ 90% of the cap.

    Returns (files_removed, bytes_freed)."""
    directory = _cache_dir()
    if not directory.is_dir():
        return 0, 0
    entries: list[tuple[float, int, Path]] = []
    total = 0
    for path in directory.iterdir():
        if not path.is_file():
            continue
        try:
            st = path.stat()
        except OSError:
            continue  # deleted underneath us
        entries.append((st.st_mtime, st.st_size, path))
        total += st.st_size
    cap = int(settings.img_cache_max_gb * 1024**3)
    if cap <= 0 or total <= cap:
        return 0, 0
    target = int(cap * 0.9)
    removed = freed = 0
    for _mtime, size, path in sorted(entries):
        if total <= target:
            break
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("img cache evict failed for %s: %s", path.name, exc)
            continue
        total -= size
        removed += 1
        freed += size
    return removed, freed


async def evict_if_needed() -> None:
    """Size check throttled to one directory scan per interval."""
    global _last_evict
    interval = max(1, settings.img_cache_evict_interval_seconds)
    if time.monotonic() - _last_evict < interval:
        return
    async with _evict_lock:
        if time.monotonic() - _last_evict < interval:
            return
        _last_evict = time.monotonic()
        try:
            removed, freed = await asyncio.to_thread(_evict_sync)
        except Exception as exc:  # noqa: BLE001 — never break the caller
            logger.warning("img cache eviction failed: %s", exc)
            return
        if removed:
            logger.info(
                "img cache evicted %d files (%.1f MB)", removed, freed / 1024**2
            )
