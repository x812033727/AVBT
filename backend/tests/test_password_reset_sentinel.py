from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as db
import app.services.auth as auth


@pytest.fixture
async def fresh_db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/auth.db", echo=False, future=True
    )
    session_local = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", session_local)
    await db.init_db()
    monkeypatch.setattr(auth, "_pwd_changed_epoch", None)
    monkeypatch.setattr(auth, "_pwd_changed_loaded", False)
    yield session_local
    await engine.dispose()


@pytest.fixture
def sentinel(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "reset_password"
    monkeypatch.setattr(auth, "_RESET_SENTINEL", path)
    return path


async def test_no_sentinel_is_a_noop(fresh_db, sentinel):
    async with fresh_db() as session:
        await auth.create_account(session, "admin", "pw")
    assert await auth.apply_reset_sentinel() is False
    async with fresh_db() as session:
        assert await auth.is_configured(session)


async def test_sentinel_drops_account_and_self_deletes(fresh_db, sentinel):
    async with fresh_db() as session:
        await auth.create_account(session, "admin", "pw")
    sentinel.write_text("")

    assert await auth.apply_reset_sentinel() is True

    assert not sentinel.exists()  # one-shot: won't re-fire next boot
    async with fresh_db() as session:
        assert not await auth.is_configured(session)
        # /setup re-arms.
        await auth.create_account(session, "admin", "new-pw")


async def test_sentinel_without_account_still_cleans_up(fresh_db, sentinel):
    sentinel.write_text("")
    assert await auth.apply_reset_sentinel() is True
    assert not sentinel.exists()
