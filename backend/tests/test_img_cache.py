"""On-disk image cache: round-trip, LRU eviction, and proxy integration."""

import os
import time

import app.routers.img as img_router
import app.services.img_cache as ic


def _use_tmp_cache(tmp_path, monkeypatch, *, max_gb=2.0):
    monkeypatch.setattr(ic.settings, "img_cache_enabled", True)
    monkeypatch.setattr(ic.settings, "img_cache_dir", str(tmp_path / "img_cache"))
    monkeypatch.setattr(ic.settings, "img_cache_max_gb", max_gb)
    monkeypatch.setattr(ic.settings, "img_cache_evict_interval_seconds", 0)
    monkeypatch.setattr(ic, "_last_evict", 0.0)


async def test_store_lookup_roundtrip(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    url = "https://www.javbus.com/pics/cover/abc.jpg"

    assert await ic.lookup(url) is None
    await ic.store(url, b"jpegbytes", "image/webp; charset=binary")
    hit = await ic.lookup(url)
    assert hit is not None
    path, media_type = hit
    assert media_type == "image/webp"
    assert path.read_bytes() == b"jpegbytes"
    assert path.suffix == ".webp"
    # A different URL is a different key.
    assert await ic.lookup(url + "?s=1") is None


async def test_concurrent_stores_of_same_url_do_not_collide(tmp_path, monkeypatch):
    # Same-URL writers used to share one <key>.tmp path: the loser's
    # os.replace raised ENOENT ("img cache store failed" noise) and could
    # even publish a half-written file. Unique temp names fix both.
    import threading

    _use_tmp_cache(tmp_path, monkeypatch)
    url = "https://www.javbus.com/pics/cover/race.jpg"
    barrier = threading.Barrier(8)
    errors = []

    def write():
        barrier.wait()
        try:
            ic._store_sync(url, b"racebytes", ".jpg")
        except Exception as exc:  # noqa: BLE001 — collected for the assert
            errors.append(exc)

    threads = [threading.Thread(target=write) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    cache_dir = tmp_path / "img_cache"
    assert [p.name for p in cache_dir.iterdir() if p.name.endswith(".tmp")] == []
    hit = await ic.lookup(url)
    assert hit is not None
    assert hit[0].read_bytes() == b"racebytes"


async def test_unknown_type_and_empty_not_stored(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    await ic.store("https://www.javbus.com/a.bmp", b"x", "image/bmp")
    await ic.store("https://www.javbus.com/b.jpg", b"", "image/jpeg")
    assert await ic.lookup("https://www.javbus.com/a.bmp") is None
    assert await ic.lookup("https://www.javbus.com/b.jpg") is None


async def test_disabled_short_circuits(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(ic.settings, "img_cache_enabled", False)
    await ic.store("https://www.javbus.com/x.jpg", b"data", "image/jpeg")
    assert await ic.lookup("https://www.javbus.com/x.jpg") is None
    assert not (tmp_path / "img_cache").exists()


async def test_eviction_removes_oldest_until_under_target(tmp_path, monkeypatch):
    # Cap ≈ 3 KB; five 1 KB files → evict down to ≤ 2.7 KB (90%).
    _use_tmp_cache(tmp_path, monkeypatch, max_gb=3 / 1024 / 1024)
    urls = [f"https://www.javbus.com/pics/{i}.jpg" for i in range(5)]
    # Store directly (bypassing evict) so we control mtimes first.
    for url in urls:
        ic._store_sync(url, b"x" * 1024, ".jpg")
    cache_dir = tmp_path / "img_cache"
    files = sorted(cache_dir.iterdir())
    now = time.time()
    for i, url in enumerate(urls):
        key_path = cache_dir / (ic._key(url) + ".jpg")
        os.utime(key_path, (now + i, now + i))  # urls[0] is oldest

    await ic.evict_if_needed()

    survivors = {p.name for p in cache_dir.iterdir()}
    assert len(survivors) == 2  # 2 KB ≤ 2.7 KB target
    # The two NEWEST mtimes survive.
    assert ic._key(urls[4]) + ".jpg" in survivors
    assert ic._key(urls[3]) + ".jpg" in survivors
    assert len(files) == 5  # sanity: all five existed before eviction


class _FakeResp:
    def __init__(self, content=b"imgbytes", ctype="image/jpeg", status=200):
        self.content = content
        self.status_code = status
        self.headers = {"content-type": ctype}


async def test_proxy_hits_disk_on_second_request(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    calls = {"n": 0}

    async def fake_safe(url):
        return True

    async def fake_fetch(url):
        calls["n"] += 1
        return _FakeResp()

    monkeypatch.setattr(img_router, "_safe_url", fake_safe)
    monkeypatch.setattr(img_router, "_fetch", fake_fetch)

    url = "https://www.javbus.com/pics/cover/hit.jpg"
    first = await img_router.proxy_image(url=url)
    assert first.headers["x-img-cache"] == "miss"
    assert calls["n"] == 1

    second = await img_router.proxy_image(url=url)
    assert second.headers["x-img-cache"] == "hit"
    assert calls["n"] == 1  # served from disk, no upstream fetch


async def test_proxy_does_not_cache_non_image(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)

    async def fake_safe(url):
        return True

    async def fake_fetch(url):
        return _FakeResp(content=b"<html>", ctype="text/html")

    monkeypatch.setattr(img_router, "_safe_url", fake_safe)
    monkeypatch.setattr(img_router, "_fetch", fake_fetch)

    url = "https://www.javbus.com/pics/challenge.jpg"
    resp = await img_router.proxy_image(url=url)
    # Coerced to jpeg for the browser, but nothing lands on disk.
    assert resp.media_type == "image/jpeg"
    assert await ic.lookup(url) is None
