import pytest

import app.services.duplicate_scan as ds


@pytest.fixture
def captured(monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ds.webhook_queue,
        "enqueue_nowait",
        lambda msg, event="generic": sent.append((event, msg)),
    )
    recorded: list[str] = []

    async def fake_record(value: str):
        recorded.append(value)

    monkeypatch.setattr(ds, "_record", fake_record)
    return sent, recorded


def _stream(events):
    async def fake(pik, pc, **kw):
        for e in events:
            yield e

    return fake


async def test_scan_notifies_when_duplicates_found(monkeypatch, captured):
    sent, recorded = captured
    result = {
        "duplicates": [{"code": f"ABC-{i:03d}"} for i in range(12)],
    }
    monkeypatch.setattr(
        ds, "find_duplicates_stream", _stream([{"type": "done", "result": result}])
    )

    out = await ds.run_scan()

    assert len(out["duplicates"]) == 12
    assert len(sent) == 1
    event, msg = sent[0]
    assert event == "duplicates_found"
    assert "ABC-000" in msg and "12" in msg
    assert recorded == ["ok:12 duplicates"]


async def test_scan_quiet_when_clean(monkeypatch, captured):
    sent, recorded = captured
    monkeypatch.setattr(
        ds,
        "find_duplicates_stream",
        _stream([{"type": "done", "result": {"duplicates": []}}]),
    )
    await ds.run_scan()
    assert sent == []
    assert recorded == ["ok:0 duplicates"]


async def test_scan_raises_on_error_event(monkeypatch, captured):
    monkeypatch.setattr(
        ds, "find_duplicates_stream", _stream([{"type": "error", "message": "boom"}])
    )
    with pytest.raises(RuntimeError, match="boom"):
        await ds.run_scan()


async def test_backup_failure_notifies(monkeypatch):
    import app.services.auto_backup as ab
    from app.services import webhook_queue as wq

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        wq.webhook_queue,
        "enqueue_nowait",
        lambda msg, event="generic": sent.append((event, msg)),
    )

    async def boom():
        raise RuntimeError("disk full")

    monkeypatch.setattr(ab, "run_backup", boom)

    async def fake_record(value: str):
        pass

    monkeypatch.setattr(ab, "_record", fake_record)
    monkeypatch.setattr(ab.settings, "auto_backup_enabled", True)

    # Drive exactly one loop iteration: patch sleep to run once then stop.
    calls = {"n": 0}

    async def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] > 1:
            raise StopAsyncIteration

    monkeypatch.setattr(ab.asyncio, "sleep", fake_sleep)
    try:
        await ab.run_loop()
    except StopAsyncIteration:
        pass

    assert sent and sent[0][0] == "backup_failed"
    assert "disk full" in sent[0][1]
