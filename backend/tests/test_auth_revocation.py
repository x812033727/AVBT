import asyncio

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
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
    # Reset the process-level revocation cache between tests.
    monkeypatch.setattr(auth, "_pwd_changed_epoch", None)
    monkeypatch.setattr(auth, "_pwd_changed_loaded", False)
    yield session_local
    await engine.dispose()


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


async def test_password_change_revokes_old_token(fresh_db):
    async with fresh_db() as session:
        await auth.create_account(session, "admin", "old-password")

    old_token = auth.create_token("admin")
    assert await auth.require_auth(_creds(old_token)) == "admin"

    # iat has second resolution — make sure the change lands visibly
    # later than the old token even with the anti-flap leeway.
    await asyncio.sleep(0)
    async with fresh_db() as session:
        assert await auth.update_password(session, "old-password", "new-password")
    auth._pwd_changed_epoch += auth._IAT_LEEWAY_S + 1  # simulate time passing

    with pytest.raises(HTTPException) as exc:
        await auth.require_auth(_creds(old_token))
    assert exc.value.status_code == 401

    new_token = auth.create_token("admin")
    auth._pwd_changed_epoch -= auth._IAT_LEEWAY_S + 1  # restore real cutoff
    assert await auth.require_auth(_creds(new_token)) == "admin"


async def test_never_changed_password_accepts_existing_tokens(fresh_db):
    async with fresh_db() as session:
        await auth.create_account(session, "admin", "pw")
    token = auth.create_token("admin")
    # Lazy load hits the DB row where password_changed_at is NULL.
    assert await auth.require_auth(_creds(token)) == "admin"
    assert auth._pwd_changed_loaded
    assert auth._pwd_changed_epoch is None


async def test_garbage_token_still_401(fresh_db):
    with pytest.raises(HTTPException):
        await auth.require_auth(_creds("not-a-jwt"))
