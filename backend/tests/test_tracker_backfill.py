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

    async def no_open_codes():
        return set()

    monkeypatch.setattr(tracker, "_recently_submitted_codes", no_open_codes)

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


async def test_backfill_skips_codes_with_a_recent_open_submission(wired, monkeypatch):
    """A code we already sent and whose row hasn't closed yet (still
    downloading, or landed but not finalized so presence can't see it)
    must not be re-sent with the next magnet on every tick. Live
    2026-11-18..20: 839 codes -> 2,475 submissions while the archiver
    was off and presence stood still."""
    queue, recorded = wired
    monkeypatch.setattr(tracker.settings, "tracker_backfill_batch_limit", 5)

    async def open_codes():
        return {"ABC-000", "ABC-002", "ABC-003"}

    monkeypatch.setattr(tracker, "_recently_submitted_codes", open_codes)

    n = await tracker._enqueue_auto_send("star", "abc", [])

    codes = [j.code for j in queue.jobs]
    # Skipped codes don't eat the batch limit — the tick still sends 5.
    assert codes == ["ABC-001", "ABC-004", "ABC-005", "ABC-006", "ABC-007"]
    assert n == 5
    assert recorded == [150]  # the dashboard count is still the true gap


async def test_new_codes_are_not_subject_to_the_open_submission_skip(wired, monkeypatch):
    queue, _ = wired
    monkeypatch.setattr(tracker.settings, "tracker_backfill_batch_limit", 1)

    async def open_codes():
        return {"NEW-001", "ABC-000"}

    monkeypatch.setattr(tracker, "_recently_submitted_codes", open_codes)

    await tracker._enqueue_auto_send("star", "abc", ["NEW-001"])

    assert [j.code for j in queue.jobs] == ["NEW-001", "ABC-001"]


async def test_open_submission_lookup_failure_does_not_block_backfill(wired, monkeypatch):
    queue, _ = wired
    monkeypatch.setattr(tracker.settings, "tracker_backfill_batch_limit", 2)

    async def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(tracker, "_recently_submitted_codes", boom)

    n = await tracker._enqueue_auto_send("star", "abc", [])

    assert n == 2


async def test_recently_submitted_codes_query(monkeypatch):
    from datetime import datetime, timedelta

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import Base
    from app.models import OfflineTaskLog

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.utcnow()

    def row(code, *, age_h=1.0, **flags):
        return OfflineTaskLog(
            code=code, magnet="m", task_id="t", file_id="", name=code,
            phase="", message="",
            created_at=now - timedelta(hours=age_h), **flags,
        )

    async with maker() as s:
        s.add_all([
            row("open-001"),                      # in flight → skip (upper-cased)
            row("OPEN-002", archived=True),       # landed, not finalized → skip
            row("DONE-001", finalized=True),      # closed → presence decides
            row("DEAD-001", abandoned=True),      # dead-lettered → retry allowed
            row("FOSS-001", superseded=True),
            row("OLD-001", age_h=30),             # past the cooldown → retry
            row(""),                              # code-less row is ignored
        ])
        await s.commit()

    monkeypatch.setattr(tracker, "SessionLocal", maker)
    monkeypatch.setattr(tracker.settings, "tracker_backfill_resend_cooldown_hours", 24)
    assert await tracker._recently_submitted_codes() == {"OPEN-001", "OPEN-002"}

    monkeypatch.setattr(tracker.settings, "tracker_backfill_resend_cooldown_hours", 0)
    assert await tracker._recently_submitted_codes() == set()
    await engine.dispose()


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
