import asyncio
from types import SimpleNamespace

import pytest

import app.services.tracker as tracker


class _FakeQueue:
    def __init__(self):
        self.jobs = []

    async def enqueue(self, job):
        self.jobs.append(job)
        fut = asyncio.get_event_loop().create_future()
        fut.set_result(SimpleNamespace(status="sent"))
        return fut


@pytest.fixture
def wired(monkeypatch):
    """_enqueue_auto_send with every external touchpoint faked."""
    queue = _FakeQueue()
    recorded: list[int] = []

    monkeypatch.setattr(tracker, "download_queue", queue)
    monkeypatch.setattr(
        tracker.presence_index, "status", lambda: {"ready": True, "last_error": ""}
    )

    missing = [SimpleNamespace(code=f"ABC-{i:03d}") for i in range(150)]
    result = SimpleNamespace(missing=missing, total=len(missing))

    async def fake_missing(kind, slug, **kw):
        return result

    monkeypatch.setattr(tracker.missing_svc, "missing_for_listing", fake_missing)

    async def fake_record(kind, slug, count):
        recorded.append(count)

    monkeypatch.setattr(tracker, "_record_scan_result", fake_record)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, model, key):
            return None  # no snapshot row → tracked_name stays ""

    monkeypatch.setattr(tracker, "SessionLocal", _FakeSession)
    # Fresh state per test.
    monkeypatch.setattr(tracker, "state", tracker.TrackerState())
    return queue, recorded


async def test_batch_limit_caps_missing_but_not_new_codes(wired, monkeypatch):
    queue, recorded = wired
    monkeypatch.setattr(tracker.settings, "tracker_backfill_batch_limit", 10)

    n = await tracker._enqueue_auto_send("star", "abc", ["NEW-001", "NEW-002"])

    assert n == 12  # 2 new + 10 capped missing
    codes = [j.code for j in queue.jobs]
    assert codes[:2] == ["NEW-001", "NEW-002"]
    assert len(codes) == 12
    assert recorded == [150]  # full missing count still recorded


async def test_backfill_disabled_sends_only_new_codes(wired, monkeypatch):
    queue, recorded = wired
    tracker.state.backfill_enabled = False

    n = await tracker._enqueue_auto_send("star", "abc", ["NEW-001"])

    assert n == 1
    assert [j.code for j in queue.jobs] == ["NEW-001"]
    assert recorded == [150]  # count refresh survives the off switch


async def test_zero_limit_means_unlimited(wired, monkeypatch):
    queue, _ = wired
    monkeypatch.setattr(tracker.settings, "tracker_backfill_batch_limit", 0)

    n = await tracker._enqueue_auto_send("star", "abc", [])

    assert n == 150
    assert len(queue.jobs) == 150


async def test_failure_rollup_writes_last_error_and_notifies(monkeypatch):
    rows = {}

    class _Row:
        last_error = ""

    row = _Row()

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, model, key):
            rows["key"] = key
            return row

        async def commit(self):
            rows["committed"] = True

    sent: list[str] = []
    monkeypatch.setattr(tracker, "SessionLocal", _FakeSession)
    monkeypatch.setattr(
        tracker.webhook_queue,
        "enqueue_nowait",
        lambda msg, event="generic": sent.append(msg),
    )

    loop = asyncio.get_event_loop()
    futs = []
    for status in ("sent", "failed", "failed"):
        f = loop.create_future()
        f.set_result(SimpleNamespace(status=status))
        futs.append(f)

    await tracker._report_auto_send_failures("star", "abc", "某女優", futs)

    assert row.last_error == "自動補檔 2/3 筆失敗"
    assert rows["committed"]
    assert sent and "某女優" in sent[0]


async def test_failure_rollup_quiet_when_all_ok(monkeypatch):
    called = []
    monkeypatch.setattr(
        tracker.webhook_queue,
        "enqueue_nowait",
        lambda msg, event="generic": called.append(msg),
    )
    loop = asyncio.get_event_loop()
    f = loop.create_future()
    f.set_result(SimpleNamespace(status="sent"))

    await tracker._report_auto_send_failures("star", "abc", "x", [f])

    assert called == []
