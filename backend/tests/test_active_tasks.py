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
from app.services.pikpak import IN_FLIGHT_PHASES, PikPakService


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


def _flooded_account(n_errors: int):
    """Fake PikPak listing: newest-first, ``n_errors`` ERROR tasks newer
    than the in-flight ones, honouring the phase filter and size cap."""
    newest_first = [
        *(SimpleNamespace(id=f"err{i}", phase="PHASE_TYPE_ERROR")
          for i in range(n_errors)),
        SimpleNamespace(id="run1", phase="PHASE_TYPE_RUNNING"),
        SimpleNamespace(id="pend1", phase="PHASE_TYPE_PENDING"),
    ]
    calls: list = []

    async def fake_list_tasks(size=100, phases=None):
        calls.append((size, phases))
        wanted = phases or ["PHASE_TYPE_RUNNING", "PHASE_TYPE_ERROR"]
        return [t for t in newest_first if t.phase in wanted][:size]

    return fake_list_tasks, calls


async def test_list_active_tasks_survives_error_flood(monkeypatch):
    """Live 2026-11-12: 30,000+ "Storage space is not enough" ERROR tasks
    sat above 10 RUNNING ones in PikPak's newest-first listing. A single
    size-capped ACTIVE_PHASES listing would have dropped the RUNNING
    tasks once ~440 more errors piled on."""
    svc = PikPakService()
    fake, calls = _flooded_account(5000)
    monkeypatch.setattr(svc, "list_tasks", fake)

    tasks = await svc.list_active_tasks(size=1000)

    assert [t.id for t in tasks[:2]] == ["run1", "pend1"]
    assert len(tasks) == 1000
    assert calls[0] == (1000, IN_FLIGHT_PHASES)
    assert calls[1] == (998, ["PHASE_TYPE_ERROR"])


async def test_list_active_tasks_skips_error_listing_when_full(monkeypatch):
    svc = PikPakService()
    calls: list = []

    async def fake_list_tasks(size=100, phases=None):
        calls.append(phases)
        return [SimpleNamespace(id=f"r{i}", phase="PHASE_TYPE_PENDING")
                for i in range(size)]

    monkeypatch.setattr(svc, "list_tasks", fake_list_tasks)

    tasks = await svc.list_active_tasks(size=3)

    assert len(tasks) == 3
    assert calls == [IN_FLIGHT_PHASES]


async def test_active_task_ids_includes_pending_behind_error_flood(monkeypatch):
    fake, _calls = _flooded_account(5000)
    monkeypatch.setattr(arch.pikpak_service, "list_tasks", fake)

    ids = await arch._active_task_ids()

    assert "pend1" in ids
    assert "run1" in ids
    assert "err0" in ids  # ERROR tasks still count as not-complete


async def test_active_task_ids_drops_complete(monkeypatch):
    async def fake_list_active_tasks(size=1000):
        return [
            SimpleNamespace(id="run1", phase="PHASE_TYPE_RUNNING"),
            SimpleNamespace(id="done1", phase="PHASE_TYPE_COMPLETE"),
        ]

    monkeypatch.setattr(
        arch.pikpak_service, "list_active_tasks", fake_list_active_tasks
    )

    assert await arch._active_task_ids() == {"run1"}


async def test_tasks_endpoint_reports_in_flight_behind_error_flood(monkeypatch):
    """The operator's in-flight view must not hide the queue.

    DB `pending` has drifted, so /api/pikpak/tasks is the only in-flight
    truth — and a task that reads as absent gets diagnosed as a dead
    seed and has its magnet resent (live: MMGO-005 VOx_Q5Tj). Same for a
    RUNNING task buried under failed ones (2026-11-12: the page showed
    500 ERROR / 0 RUNNING with 10 tasks still downloading).
    """
    fake, _calls = _flooded_account(5000)
    monkeypatch.setattr(pikpak_router.pikpak_service, "list_tasks", fake)

    tasks = await pikpak_router.list_tasks(size=500)

    assert [t.id for t in tasks[:2]] == ["run1", "pend1"]
    assert len(tasks) == 500
