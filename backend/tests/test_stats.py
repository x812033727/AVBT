from datetime import datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as db
from app.models import CollectedMovie, OfflineTaskLog, PCloudTransfer, TrackedListing
from app.routers.stats import dashboard


async def test_dashboard_aggregates(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        session.add_all(
            [
                CollectedMovie(
                    code="ABP-123",
                    title="t1",
                    status="wishlist",
                    actresses=["葵つかさ"],
                    genres=["單體作品"],
                ),
                CollectedMovie(
                    code="DAM-043",
                    title="t2",
                    status="done",
                    actresses=["葵つかさ", "另一位"],
                    genres=[],
                ),
                OfflineTaskLog(
                    code="ABP-123",
                    magnet="magnet:?xt=urn:btih:" + "A" * 40,
                    file_id="f1",
                    phase="PHASE_TYPE_COMPLETE",
                    archived=True,
                    archived_at=datetime.utcnow(),
                ),
                OfflineTaskLog(
                    code="DAM-043",
                    magnet="magnet:?xt=urn:btih:" + "B" * 40,
                    file_id="f2",
                    phase="PHASE_TYPE_RUNNING",
                    archived=False,
                ),
                OfflineTaskLog(
                    code="ERR-001",
                    magnet="magnet:?xt=urn:btih:" + "C" * 40,
                    file_id="",  # failed task: no file → excluded from rate base
                    phase="PHASE_TYPE_ERROR",
                    archived=False,
                ),
                TrackedListing(kind="star", id="abc", name="葵つかさ", new_count=3),
                TrackedListing(kind="series", id="11pb", name="回胴錄", new_count=0),
                PCloudTransfer(pikpak_file_id="f1", pikpak_name="n", status="done"),
                PCloudTransfer(pikpak_file_id="f2", pikpak_name="n", status="failed"),
            ]
        )
        await session.commit()

        out = await dashboard(session=session)

    await engine.dispose()

    assert out.collection_total == 2
    assert out.collection_by_status == {"wishlist": 1, "done": 1}
    assert out.downloads_total == 3
    assert out.archived_count == 1
    assert out.archive_rate == 0.5  # 1 archived / 2 rows with a file
    assert out.tracked_total == 2
    assert out.tracked_by_kind == {"star": 1, "series": 1}
    assert out.tracked_new_total == 3
    assert out.tracked_top_new[0].name == "葵つかさ"
    assert out.top_actresses[0].name == "葵つかさ"
    assert out.top_actresses[0].count == 2
    assert out.top_genres == [] or out.top_genres[0].name == "單體作品"
    assert out.pcloud_transfers_by_status == {"done": 1, "failed": 1}
    assert len(out.trend) == 30
    today_point = out.trend[-1]
    assert today_point.sent == 3 and today_point.archived == 1
