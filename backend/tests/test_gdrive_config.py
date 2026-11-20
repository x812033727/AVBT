"""Settings-page Drive configuration: key storage/rotation, OAuth clients,
redirect URI, legacy .env import, per-client OAuth start/callback."""

import os
import stat
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.database as db
import app.routers.gdrive as router
import app.services.gdrive_accounts as ga
import app.services.gdrive_config as gc
from app.config import settings
from app.models import GDriveAccount, GDriveOAuthClient
from app.services import gdrive_crypto


@pytest.fixture()
async def maker(tmp_path, monkeypatch):
    monkeypatch.setattr(gdrive_crypto, "KEY_FILE", tmp_path / "data" / "gdrive_token_key.txt")
    gdrive_crypto.reset_cache()
    for k in ("gdrive_token_key", "gdrive_client_id", "gdrive_client_secret"):
        monkeypatch.setattr(settings, k, "")
    monkeypatch.setattr(settings, "gdrive_redirect_uri", gc.DEFAULT_REDIRECT)
    monkeypatch.setattr(settings, "auth_secret", "test-secret")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/c.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    for mod in (gc, ga, router):
        monkeypatch.setattr(mod, "SessionLocal", m)
    yield m
    gdrive_crypto.reset_cache()
    await engine.dispose()


# ---------- key ----------


async def test_key_generated_into_data_file_with_0600(maker):
    assert gdrive_crypto.key_source() == "none"
    assert await gc.set_or_rotate_key(None) == "file"
    f = gdrive_crypto.KEY_FILE
    assert f.is_file()
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert gdrive_crypto.key_source() == "file"
    assert gdrive_crypto.decrypt(gdrive_crypto.encrypt("x")) == "x"


async def test_env_key_wins_and_blocks_ui_change(maker, monkeypatch):
    await gc.set_or_rotate_key(None)
    monkeypatch.setattr(settings, "gdrive_token_key", gdrive_crypto.generate_key())
    assert gdrive_crypto.key_source() == "env"
    with pytest.raises(gc.ConfigConflict):
        await gc.set_or_rotate_key(None)


async def test_invalid_custom_key_rejected(maker):
    with pytest.raises(gc.ConfigError):
        await gc.set_or_rotate_key("not-a-fernet-key")
    assert gdrive_crypto.key_source() == "none"


async def test_rotate_reencrypts_every_secret(maker):
    await gc.set_or_rotate_key(None)
    client = await gc.create_client(label="P1", client_id="id-1", client_secret="secret-1")
    async with maker() as s:
        s.add(GDriveAccount(label="a", refresh_token_enc=gdrive_crypto.encrypt("rt-1"),
                            oauth_client_id=client["id"]))
        await s.commit()
    old = gdrive_crypto.current_key()
    new = gdrive_crypto.generate_key()
    await gc.set_or_rotate_key(new)
    assert gdrive_crypto.current_key() == new != old
    async with maker() as s:
        acc = (await s.execute(select(GDriveAccount))).scalars().one()
        cl = (await s.execute(select(GDriveOAuthClient))).scalars().one()
    assert gdrive_crypto.decrypt(acc.refresh_token_enc) == "rt-1"
    assert gdrive_crypto.decrypt(cl.client_secret_enc) == "secret-1"
    with pytest.raises(gdrive_crypto.TokenKeyError):
        gdrive_crypto.decrypt(acc.refresh_token_enc, old)


async def test_rotate_aborts_without_writing_when_undecryptable(maker):
    await gc.set_or_rotate_key(None)
    stranger = gdrive_crypto.generate_key()
    async with maker() as s:
        s.add(GDriveAccount(label="a", refresh_token_enc=gdrive_crypto.encrypt("rt", stranger)))
        await s.commit()
    before = gdrive_crypto.current_key()
    with pytest.raises(gc.ConfigConflict):
        await gc.set_or_rotate_key(gdrive_crypto.generate_key())
    gdrive_crypto.reset_cache()
    assert gdrive_crypto.current_key() == before  # file untouched


# ---------- clients ----------


async def test_client_crud_never_exposes_secret(maker):
    with pytest.raises(gc.ConfigConflict):  # no key yet
        await gc.create_client(label="x", client_id="id", client_secret="s")
    await gc.set_or_rotate_key(None)
    c = await gc.create_client(label="專案一", client_id="id-1.apps", client_secret="GOCSPX-abcd1234")
    assert "GOCSPX" not in str(c) and c["client_secret_masked"] == "••••1234"
    listed = await gc.list_clients()
    assert "GOCSPX" not in str(listed)
    # Empty secret on update keeps the old one.
    c2 = await gc.update_client(c["id"], label="改名", client_secret="")
    assert c2["label"] == "改名" and c2["client_secret_masked"] == "••••1234"
    assert (await gc.creds_for(c["id"])).client_secret == "GOCSPX-abcd1234"
    # Disabled client can't be used.
    await gc.update_client(c["id"], enabled=False)
    with pytest.raises(gc.ConfigConflict):
        await gc.creds_for(c["id"])


async def test_delete_client_blocked_while_accounts_bound(maker):
    await gc.set_or_rotate_key(None)
    c = await gc.create_client(label="p", client_id="id", client_secret="s")
    async with maker() as s:
        s.add(GDriveAccount(label="a", oauth_client_id=c["id"]))
        await s.commit()
    with pytest.raises(gc.ConfigConflict):
        await gc.delete_client(c["id"])


async def test_redirect_uri_sources(maker, monkeypatch):
    assert await gc.redirect_uri() == (gc.DEFAULT_REDIRECT, "default")
    with pytest.raises(gc.ConfigError):
        await gc.set_redirect_uri("https://x.example/wrong")
    await gc.set_redirect_uri("https://api.example.com/api/gdrive/oauth/callback/")
    assert await gc.redirect_uri() == ("https://api.example.com/api/gdrive/oauth/callback", "settings")
    monkeypatch.setattr(settings, "gdrive_redirect_uri", "https://env.example/api/gdrive/oauth/callback")
    assert (await gc.redirect_uri())[1] == "env"


async def test_import_env_client_binds_legacy_accounts(maker, monkeypatch):
    monkeypatch.setattr(settings, "gdrive_client_id", "legacy-id")
    monkeypatch.setattr(settings, "gdrive_client_secret", "legacy-secret")
    with pytest.raises(RuntimeError):  # no key → retry next boot
        await gc.import_env_client()
    await gc.set_or_rotate_key(None)
    async with maker() as s:
        s.add(GDriveAccount(label="old"))
        await s.commit()
    await gc.import_env_client()
    await gc.import_env_client()  # idempotent
    clients = await gc.list_clients()
    assert len(clients) == 1 and clients[0]["client_id"] == "legacy-id"
    async with maker() as s:
        acc = (await s.execute(select(GDriveAccount))).scalars().one()
    assert acc.oauth_client_id == clients[0]["id"]


# ---------- OAuth start / callback per client ----------


async def test_oauth_start_uses_selected_client(maker):
    await gc.set_or_rotate_key(None)
    a = await gc.create_client(label="A", client_id="client-A", client_secret="sa")
    b = await gc.create_client(label="B", client_id="client-B", client_secret="sb")
    with pytest.raises(HTTPException):  # two clients, none chosen
        await router.oauth_start(router.OAuthStart(label="x"))
    res = await router.oauth_start(router.OAuthStart(label="x", oauth_client_id=b["id"]))
    q = parse_qs(urlparse(res["url"]).query)
    assert q["client_id"] == ["client-B"]
    assert ga.read_state(q["state"][0])["cid"] == b["id"]
    await gc.update_client(b["id"], enabled=False)
    res = await router.oauth_start(router.OAuthStart(label="x"))  # only A left
    assert parse_qs(urlparse(res["url"]).query)["client_id"] == ["client-A"]
    assert a["id"]


async def test_callback_exchanges_with_that_client(maker, monkeypatch):
    await gc.set_or_rotate_key(None)
    await gc.create_client(label="A", client_id="client-A", client_secret="sa")
    b = await gc.create_client(label="B", client_id="client-B", client_secret="sb")
    seen = {}

    async def exchange(code, creds, redirect):
        seen["creds"] = creds
        return {"refresh_token": "rt-new", "access_token": "at"}

    async def about(access):
        return {"email": "u@x", "name": "", "quota_total": 1, "quota_used": 0}

    monkeypatch.setattr(ga.gdrive_client, "exchange_code", exchange)
    monkeypatch.setattr(ga.gdrive_client, "about_with_access_token", about)
    acc = await ga.upsert_from_oauth(code="c", label="L", account_id=None, client_pk=b["id"])
    assert seen["creds"].client_id == "client-B" and seen["creds"].client_secret == "sb"
    assert acc.oauth_client_id == b["id"]
    auth = await ga.credentials(acc.id)
    assert auth.refresh_token == "rt-new" and auth.creds.client_id == "client-B"
