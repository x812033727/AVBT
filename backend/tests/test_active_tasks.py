"""PENDING tasks must count as active.

PikPak caps concurrent offline tasks at ~100; anything submitted beyond
that queues as PHASE_TYPE_PENDING. pikpakapi's default phase filter is
RUNNING+ERROR and each page caps at 100, so a deep queue was invisible
to ``_active_task_ids`` — the finalize retry pass then stamped queued
upgrade rows against the pre-upgrade files and the orphan reaper closed
them as "task gone" (live: MMGO-005 rows 20521/20899).
"""

from types import SimpleNamespace

import app.routers.pikpak as pikpak_router
import app.services.archiver as arch
from app.services.pikpak import ACTIVE_PHASES, PikPakService


async def test_list_tasks_paginates_and_forwards_phases(monkeypatch):
    calls: list[tuple[str | None, tuple[str, ...]]] = []

    class FakeClient:
        async def offline_list(self, size=100, next_page_token=None, phase=None):
            calls.append((next_page_token, tuple(phase or ())))
            if next_page_token is None:
                return {
                    "tasks": [
                        {"id": f"t{i}", "phase": "PHASE_TYPE_RUNNING"}
                        for i in range(100)
                    ],
                    "next_page_token": "page2",
                }
            return {
                "tasks": [{"id": "t100", "phase": "PHASE_TYPE_PENDING"}],
                "next_page_token": "",
            }

    svc = PikPakService()

    async def fake_call(op):
        return await op(FakeClient())

    monkeypatch.setattr(svc, "_call", fake_call)

    phases = ["PHASE_TYPE_RUNNING", "PHASE_TYPE_PENDING"]
    tasks = await svc.list_tasks(size=1000, phases=phases)

    assert [t.id for t in tasks[:2]] == ["t0", "t1"]
    assert len(tasks) == 101
    assert tasks[-1].id == "t100"
    # Every page must carry the widened phase filter and follow the token.
    assert [tk for tk, _ in calls] == [None, "page2"]
    assert all(ph == tuple(phases) for _, ph in calls)


async def test_list_tasks_stops_at_requested_size(monkeypatch):
    class FakeClient:
        async def offline_list(self, size=100, next_page_token=None, phase=None):
            return {
                "tasks": [
                    {"id": f"{next_page_token or 'p1'}-{i}",
                     "phase": "PHASE_TYPE_RUNNING"}
                    for i in range(100)
                ],
                "next_page_token": "more",
            }

    svc = PikPakService()

    async def fake_call(op):
        return await op(FakeClient())

    monkeypatch.setattr(svc, "_call", fake_call)

    tasks = await svc.list_tasks(size=150)
    assert len(tasks) == 150


async def test_active_task_ids_includes_pending(monkeypatch):
    captured: dict = {}

    async def fake_list_tasks(size=100, phases=None):
        captured["phases"] = phases
        return [
            SimpleNamespace(id="pend1", phase="PHASE_TYPE_PENDING"),
            SimpleNamespace(id="run1", phase="PHASE_TYPE_RUNNING"),
            SimpleNamespace(id="done1", phase="PHASE_TYPE_COMPLETE"),
        ]

    monkeypatch.setattr(arch.pikpak_service, "list_tasks", fake_list_tasks)

    ids = await arch._active_task_ids()

    assert "pend1" in ids
    assert "run1" in ids
    assert "done1" not in ids
    assert "PHASE_TYPE_PENDING" in (captured["phases"] or [])


async def test_tasks_endpoint_reports_pending(monkeypatch):
    """The operator's in-flight view must not hide the queue.

    DB `pending` has drifted, so /api/pikpak/tasks is the only in-flight
    truth — and a queued task that reads as absent gets diagnosed as a
    dead seed and has its magnet resent (live: MMGO-005 VOx_Q5Tj).
    """
    captured: dict = {}

    async def fake_list_tasks(size=100, phases=None):
        captured["phases"] = phases
        return [
            SimpleNamespace(id="run1", phase="PHASE_TYPE_RUNNING"),
            SimpleNamespace(id="pend1", phase="PHASE_TYPE_PENDING"),
        ]

    monkeypatch.setattr(pikpak_router.pikpak_service, "list_tasks", fake_list_tasks)

    tasks = await pikpak_router.list_tasks(size=500)

    assert captured["phases"] == ACTIVE_PHASES
    assert [t.id for t in tasks] == ["run1", "pend1"]
