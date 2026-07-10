import asyncio

from app.services.supervisor import supervise


async def test_crashing_task_is_restarted():
    runs = 0

    async def crashy():
        nonlocal runs
        runs += 1
        raise RuntimeError("boom")

    task = supervise(crashy, "crashy", restart_delay=0.01)
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert runs >= 2  # died and came back at least once


async def test_graceful_stop_is_not_resurrected():
    stopped = asyncio.Event()
    runs = 0

    async def loop():
        nonlocal runs
        runs += 1
        await stopped.wait()

    task = supervise(
        loop, "graceful", restart_delay=0.01,
        should_restart=lambda: not stopped.is_set(),
    )
    await asyncio.sleep(0.02)
    stopped.set()
    await asyncio.wait_for(task, timeout=1.0)  # ends by itself
    assert runs == 1


async def test_cancellation_passes_through():
    started = asyncio.Event()

    async def forever():
        started.set()
        await asyncio.Event().wait()

    task = supervise(forever, "forever", restart_delay=0.01)
    await started.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task.cancelled()
