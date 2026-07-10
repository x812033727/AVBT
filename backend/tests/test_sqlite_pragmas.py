from sqlalchemy.ext.asyncio import create_async_engine

import app.database as db


async def _pragma(conn, name: str):
    return (await conn.exec_driver_sql(f"PRAGMA {name}")).scalar()


async def test_pragmas_applied_on_file_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/test.db", echo=False, future=True
    )
    db.attach_sqlite_pragmas(engine)

    async with engine.connect() as conn:
        assert await _pragma(conn, "journal_mode") == "wal"
        assert await _pragma(conn, "busy_timeout") == 30000
        # NORMAL == 1
        assert await _pragma(conn, "synchronous") == 1
    await engine.dispose()


async def test_wal_persists_and_reapplies_on_new_connection(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    engine = create_async_engine(url, echo=False, future=True)
    db.attach_sqlite_pragmas(engine)
    async with engine.connect() as conn:
        assert await _pragma(conn, "journal_mode") == "wal"
    await engine.dispose()

    # journal_mode stuck in the db file; busy_timeout is per-connection
    # and must come back through the connect hook on a fresh pool.
    engine2 = create_async_engine(url, echo=False, future=True)
    db.attach_sqlite_pragmas(engine2)
    async with engine2.connect() as conn:
        assert await _pragma(conn, "journal_mode") == "wal"
        assert await _pragma(conn, "busy_timeout") == 30000
    await engine2.dispose()


async def test_module_engine_has_pragma_hook():
    # The app engine is built against DATABASE_URL (in-memory under
    # tests — journal_mode there reports "memory") but the connect hook
    # must still be attached and applied without erroring.
    async with db.engine.connect() as conn:
        assert await _pragma(conn, "busy_timeout") == 30000
