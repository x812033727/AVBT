from sqlalchemy.ext.asyncio import create_async_engine

import app.database as db


async def _fresh_engine(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/test.db", echo=False, future=True
    )
    return engine


async def test_init_db_sets_flags_and_indexes(tmp_path, monkeypatch):
    engine = await _fresh_engine(tmp_path)
    monkeypatch.setattr(db, "engine", engine)

    await db.init_db()

    async with engine.begin() as conn:
        flags = {
            k
            for (k, v) in (
                await conn.exec_driver_sql("SELECT key, value FROM app_meta")
            ).all()
            if v == "1"
        }
        indexes = {
            r[0]
            for r in (
                await conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            ).all()
        }
    await engine.dispose()

    assert {
        "migrated:tracked_actresses_backfill",
        "migrated:btih_backfill",
        "migrated:listing_name_cleanup",
    } <= flags
    assert {
        "ix_offline_task_log_created_at",
        "ix_offline_task_log_phase",
        "ix_collected_movies_status",
        "ix_collected_movies_updated_at",
        "ix_pcloud_transfer_created_at",
        "ix_movie_detail_cache_fetched_at",
    } <= indexes


async def test_second_boot_skips_one_time_migrations(tmp_path, monkeypatch):
    engine = await _fresh_engine(tmp_path)
    monkeypatch.setattr(db, "engine", engine)

    calls = {"n": 0}
    real = db._backfill_btih

    async def counting():
        calls["n"] += 1
        await real()

    monkeypatch.setattr(db, "_backfill_btih", counting)

    await db.init_db()
    assert calls["n"] == 1
    await db.init_db()
    assert calls["n"] == 1  # guarded by the app_meta flag

    await engine.dispose()


async def test_btih_backfill_fills_rows_and_terminates(tmp_path, monkeypatch):
    engine = await _fresh_engine(tmp_path)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()

    btih = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "INSERT INTO offline_task_log "
            "(code, magnet, btih, task_id, file_id, name, phase, message,"
            " archived, tracked_kind, tracked_slug, tracked_name, created_at) VALUES "
            f"('AAA-001', 'magnet:?xt=urn:btih:{btih}', '', '', '', '', '', '', 0, '', '', '', CURRENT_TIMESTAMP), "
            # Yields no hash — must not loop forever.
            "('BBB-002', 'not-a-magnet', '', '', '', '', '', '', 0, '', '', '', CURRENT_TIMESTAMP)"
        )
        await conn.exec_driver_sql(
            "DELETE FROM app_meta WHERE key = 'migrated:btih_backfill'"
        )

    await db.init_db()

    async with engine.begin() as conn:
        rows = dict(
            (
                await conn.exec_driver_sql("SELECT code, btih FROM offline_task_log")
            ).all()
        )
    await engine.dispose()

    assert rows["AAA-001"] == btih
    assert rows["BBB-002"] == ""


async def test_legacy_tracked_actresses_copied_once(tmp_path, monkeypatch):
    engine = await _fresh_engine(tmp_path)
    monkeypatch.setattr(db, "engine", engine)

    # Simulate a pre-TrackedListing database: legacy table with one row.
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            """
            CREATE TABLE tracked_actresses (
                id VARCHAR(64) PRIMARY KEY,
                name VARCHAR(128) DEFAULT '',
                avatar VARCHAR(1024) DEFAULT '',
                uncensored BOOLEAN DEFAULT 0,
                auto_send BOOLEAN DEFAULT 0,
                last_seen_code VARCHAR(64) DEFAULT '',
                last_checked_at DATETIME,
                last_error TEXT DEFAULT '',
                new_count INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.exec_driver_sql(
            "INSERT INTO tracked_actresses (id, name) VALUES ('abc', '葵つかさ')"
        )

    await db.init_db()

    async with engine.begin() as conn:
        rows = (
            await conn.exec_driver_sql(
                "SELECT kind, id, name FROM tracked_listing"
            )
        ).all()
        # The physical legacy table survives (kept as a safety net).
        legacy = (
            await conn.exec_driver_sql(
                "SELECT COUNT(*) FROM tracked_actresses"
            )
        ).scalar_one()
    await engine.dispose()

    assert ("star", "abc", "葵つかさ") in [tuple(r) for r in rows]
    assert legacy == 1
