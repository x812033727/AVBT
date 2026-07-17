"""_poll_running polls running rows concurrently, bounded by
pcloud_poll_concurrency (default 1 = serial). One row's error never aborts
the pass; the whole per-row body runs inside the semaphore."""

import asyncio

import app.services.pcloud_transfer as pt


async def _run_with(monkeypatch, rows, concurrency, progress):
    monkeypatch.setattr(pt.settings, "pcloud_poll_concurrency", concurrency)

    # Feed rows straight into _poll_running by stubbing the DB read.
    class _Result:
        def all(self_):
            return rows

    class _Sess:
        async def __aenter__(self_):
            return self_
        async def __aexit__(self_, *a):
            return False
        async def execute(self_, *a, **k):
            return _Result()
        async def commit(self_):
            return None

    monkeypatch.setattr(pt, "SessionLocal", lambda: _Sess())
    monkeypatch.setattr(pt.pcloud_service, "upload_progress", progress)
    return pt.PCloudTransferQueue()


async def test_poll_bounds_concurrency(monkeypatch):
    active = 0
    peak = 0

    async def progress(uid):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return {"status": "downloading", "downloaded": 1, "size": 2}

    rows = [(_i, _i, False, "", "") for _i in range(8)]
    svc = await _run_with(monkeypatch, rows, 3, progress)
    await svc._poll_running()
    assert peak <= 3


async def test_poll_isolates_row_error(monkeypatch):
    seen = []

    async def progress(uid):
        seen.append(uid)
        if uid == 2:
            raise RuntimeError("pcloud hiccup")
        return {"status": "downloading", "downloaded": 1, "size": 2}

    rows = [(i, i, False, "", "") for i in range(4)]
    svc = await _run_with(monkeypatch, rows, 2, progress)
    await svc._poll_running()          # must not raise
    assert set(seen) == {0, 1, 2, 3}   # every row polled despite one error


async def test_poll_serial_default(monkeypatch):
    active = 0
    peak = 0

    async def progress(uid):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return {"status": "downloading", "downloaded": 1, "size": 2}

    rows = [(i, i, False, "", "") for i in range(5)]
    svc = await _run_with(monkeypatch, rows, 1, progress)  # default
    await svc._poll_running()
    assert peak == 1                   # serial
