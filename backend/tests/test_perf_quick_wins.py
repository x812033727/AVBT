from types import SimpleNamespace

import app.services.download_queue as dq
from app.routers.collection import sent_hashes
from app.services.pcloud_jobs import OrganizeJobManager


async def test_sent_hashes_served_from_cache(monkeypatch):
    monkeypatch.setattr(dq, "_sent_hashes_cache", {"beef", "cafe"})
    assert await sent_hashes() == ["beef", "cafe"]


async def test_direct_submit_dedups_via_cache_without_db(monkeypatch):
    magnet = "magnet:?xt=urn:btih:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    h = dq.extract_btih(magnet)
    assert h
    monkeypatch.setattr(dq, "_sent_hashes_cache", {h})

    # SessionLocal must never be touched on the cache-hit path.
    def _boom():
        raise AssertionError("DB hit on cache path")

    monkeypatch.setattr(dq, "SessionLocal", _boom)

    job = SimpleNamespace(code="ABC-123", direct_magnet=magnet, force=False)
    result = await dq.download_queue._process_direct(job)
    assert result.status == "skipped_already_sent"


def _finish(job, at: str):
    job.status = "done"
    job.finished_at = at


def test_finished_jobs_pruned_oldest_first():
    mgr = OrganizeJobManager(keep_finished=2)
    j1 = mgr.create("f1", "n1", True)
    j2 = mgr.create("f2", "n2", True)
    j3 = mgr.create("f3", "n3", True)
    running = mgr.create("f4", "n4", True)  # stays running
    _finish(j1, "2026-01-01T00:00:00")
    _finish(j2, "2026-01-02T00:00:00")
    _finish(j3, "2026-01-03T00:00:00")

    mgr.create("f5", "n5", True)  # triggers prune

    assert mgr.get(j1.job_id) is None  # oldest finished dropped
    assert mgr.get(j2.job_id) is not None
    assert mgr.get(j3.job_id) is not None
    assert mgr.get(running.job_id) is not None  # running never pruned
