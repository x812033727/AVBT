"""tasks/delete must report what happened. PikPak's delete API answers
null whether the ids existed or not, so a truncated id used to read as
success while the task stayed put (live 2026-07-17 r40)."""

from types import SimpleNamespace

import app.routers.pikpak as router


class Svc:
    """Task list with ids; delete removes only ids that exist."""

    def __init__(self, ids):
        self._ids = set(ids)
        self.delete_calls = []

    async def list_tasks(self, size=100):
        return [SimpleNamespace(id=i) for i in sorted(self._ids)]

    async def delete_tasks(self, task_ids, delete_files=False):
        self.delete_calls.append(list(task_ids))
        self._ids -= set(task_ids)
        return None  # what the real API answers, success or not


async def test_reports_deleted_vs_not_found(monkeypatch):
    svc = Svc({"VOx-full-id-1", "VOx-full-id-2"})
    monkeypatch.setattr(router, "pikpak_service", svc)

    out = await router.delete_tasks(
        task_ids=["VOx-full-id-1", "VOx-trunc"], delete_files=False
    )
    assert out["deleted"] == ["VOx-full-id-1"]
    # The truncated id was never listed — the old null reply hid this.
    assert out["not_found"] == ["VOx-trunc"]
    assert out["still_listed"] == []
    assert svc.delete_calls == [["VOx-full-id-1", "VOx-trunc"]]


async def test_reports_survivor_as_still_listed(monkeypatch):
    class StubbornSvc(Svc):
        async def delete_tasks(self, task_ids, delete_files=False):
            self.delete_calls.append(list(task_ids))
            return None  # pretends to work, removes nothing

    svc = StubbornSvc({"VOx-full-id-1"})
    monkeypatch.setattr(router, "pikpak_service", svc)

    out = await router.delete_tasks(task_ids=["VOx-full-id-1"])
    assert out["deleted"] == []
    assert out["still_listed"] == ["VOx-full-id-1"]
