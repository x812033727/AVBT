"""Round-4 "ops badges": ArchiverState.to_dict() exposes the round-2
concurrency knobs (pure settings reads, no DB), and GET /api/pikpak/archiver
layers on a DB count of abandoned (dead-letter) rows so the frontend can
badge both without a second round-trip."""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as db
import app.routers.pikpak as router
import app.services.archiver as archiver
from app.config import settings
from app.models import OfflineTaskLog


def test_to_dict_exposes_concurrency_knobs(monkeypatch):
    monkeypatch.setattr(settings, "archive_finalize_concurrency", 4)
    monkeypatch.setattr(settings, "pcloud_poll_concurrency", 2)

    out = archiver.state.to_dict()

    assert out["finalize_concurrency"] == 4
    assert out["pcloud_poll_concurrency"] == 2


async def test_archiver_status_reports_abandoned_total(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(router, "SessionLocal", maker)

    async with maker() as session:
        session.add_all(
            [
                OfflineTaskLog(
                    code="DEAD-001",
                    magnet="magnet:?xt=urn:btih:" + "A" * 40,
                    file_id="",
                    phase="PHASE_TYPE_ERROR",
                    archived=False,
                    abandoned=True,
                ),
                OfflineTaskLog(
                    code="DEAD-002",
                    magnet="magnet:?xt=urn:btih:" + "B" * 40,
                    file_id="stale",
                    phase="",
                    archived=False,
                    abandoned=True,
                ),
                OfflineTaskLog(
                    code="LIVE-001",
                    magnet="magnet:?xt=urn:btih:" + "C" * 40,
                    file_id="f1",
                    phase="PHASE_TYPE_COMPLETE",
                    archived=True,
                    abandoned=False,
                ),
            ]
        )
        await session.commit()

    out = await router.archiver_status()
    await engine.dispose()

    assert out["abandoned_total"] == 2
    # Existing to_dict() keys must still be present — archiver_status
    # layers the DB count on top, it doesn't replace anything.
    assert "enabled" in out
    assert "finalize_concurrency" in out
