"""Google Drive: accounts (OAuth), traffic policy, per-studio move jobs,
and code → Drive location lookup for the movie page player.

``public_router`` holds only the OAuth callback — Google redirects the
browser there without our Authorization header; the signed ``state``
JWT is what authenticates it (services/gdrive_accounts.make_state).
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..config import cors_origin_list, settings
from ..database import SessionLocal
from ..models import GDriveAccount, GDriveJob, GDriveLocation, GDriveTransfer
from ..services import gdrive_accounts, gdrive_config, gdrive_crypto, gdrive_policy, studio_index
from ..services.gdrive import (
    GDriveError,
    build_auth_url,
    file_preview_url,
    file_view_url,
    folder_url,
    gdrive_client,
)
from ..services.gdrive_transfer import ACTIVE_JOB_STATES, gdrive_transfer_queue
from ..services.jav_code import normalize_code

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/gdrive", tags=["gdrive"])
public_router = APIRouter(prefix="/api/gdrive", tags=["gdrive"])


async def _require_ready() -> None:
    if not gdrive_crypto.key_configured():
        raise HTTPException(status_code=400, detail="尚未設定加密金鑰(設定頁 → Google Drive)")
    if not await gdrive_config.any_client_enabled():
        raise HTTPException(status_code=400, detail="尚未新增(或全部停用)OAuth 用戶端(設定頁 → Google Drive)")


def _frontend_settings_url(**params: str) -> str:
    base = (settings.gdrive_frontend_url or "").strip()
    if not base:
        origins = [o for o in cors_origin_list() if o != "*"]
        base = (origins[0] if origins else "http://localhost:3000").rstrip("/") + "/settings"
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode(params)}" if params else base


# ---------- status / accounts ----------


@router.get("/status")
async def status():
    redirect, _src = await gdrive_config.redirect_uri()
    return {
        "oauth_configured": await gdrive_config.any_client_enabled(),
        "key_configured": gdrive_crypto.key_configured(),
        "redirect_uri": redirect,
        "accounts": await gdrive_accounts.list_public(),
        "queue": await gdrive_transfer_queue.status(),
    }


@router.get("/accounts")
async def list_accounts():
    return await gdrive_accounts.list_public()


class OAuthStart(BaseModel):
    label: str = ""
    account_id: int | None = None  # set = re-authorise this account
    # Which OAuth client to authorise with. Omitted: the account's current
    # client (re-authorise) or the only enabled client.
    oauth_client_id: int | None = None


@router.post("/oauth/start")
async def oauth_start(payload: OAuthStart):
    await _require_ready()
    client_pk = payload.oauth_client_id or 0
    if payload.account_id:
        async with SessionLocal() as session:
            acc = await session.get(GDriveAccount, payload.account_id)
        if acc is None:
            raise HTTPException(status_code=404, detail="帳號不存在")
        client_pk = client_pk or acc.oauth_client_id
    if not client_pk:
        enabled = [c for c in await gdrive_config.list_clients() if c["enabled"]]
        if len(enabled) != 1:
            raise HTTPException(status_code=400, detail="請選擇要使用的 OAuth 用戶端")
        client_pk = enabled[0]["id"]
    try:
        creds = await gdrive_config.creds_for(client_pk)
    except gdrive_config.ConfigConflict as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    redirect, _src = await gdrive_config.redirect_uri()
    state = gdrive_accounts.make_state(
        client_pk=client_pk, label=payload.label.strip(), account_id=payload.account_id
    )
    return {"url": build_auth_url(state, creds, redirect)}


@public_router.get("/oauth/callback")
async def oauth_callback(
    code: str = Query(""), state: str = Query(""), error: str = Query("")
):
    if error:
        return RedirectResponse(_frontend_settings_url(gdrive="error", msg=f"Google 拒絕授權: {error}"))
    claims = gdrive_accounts.read_state(state)
    if claims is None:
        return RedirectResponse(_frontend_settings_url(gdrive="error", msg="授權連結已過期或無效,請重新新增"))
    if not code:
        return RedirectResponse(_frontend_settings_url(gdrive="error", msg="Google 沒有回傳授權碼"))
    try:
        acc = await gdrive_accounts.upsert_from_oauth(
            code=code,
            label=claims.get("label", ""),
            account_id=claims.get("aid") or None,
            client_pk=int(claims.get("cid") or 0),
        )
    except Exception as exc:  # noqa: BLE001 — report back to the UI
        logger.warning("gdrive oauth callback failed: %s", exc)
        return RedirectResponse(_frontend_settings_url(gdrive="error", msg=str(exc)[:300]))
    return RedirectResponse(_frontend_settings_url(gdrive="ok", msg=acc.email or acc.label))


# ---------- OAuth configuration (settings page) ----------


def _config_error(exc: Exception) -> HTTPException:
    code = 409 if isinstance(exc, gdrive_config.ConfigConflict) else 400
    return HTTPException(status_code=code, detail=str(exc))


@router.get("/config")
async def get_config():
    redirect, redirect_src = await gdrive_config.redirect_uri()
    return {
        "key_source": gdrive_crypto.key_source(),
        "key_configured": gdrive_crypto.key_configured(),
        "redirect_uri": redirect,
        "redirect_source": redirect_src,
        "clients": await gdrive_config.list_clients(),
    }


class RedirectIn(BaseModel):
    redirect_uri: str


@router.put("/config/redirect-uri")
async def put_redirect_uri(payload: RedirectIn):
    _uri, src = await gdrive_config.redirect_uri()
    if src == "env":
        raise HTTPException(status_code=409, detail="重新導向網址目前來自 .env(GDRIVE_REDIRECT_URI),請改 .env")
    try:
        return {"redirect_uri": await gdrive_config.set_redirect_uri(payload.redirect_uri)}
    except gdrive_config.ConfigError as exc:
        raise _config_error(exc) from exc


class TokenKeyIn(BaseModel):
    key: str | None = None  # empty = generate one


@router.post("/config/token-key")
async def put_token_key(payload: TokenKeyIn):
    try:
        source = await gdrive_config.set_or_rotate_key((payload.key or "").strip() or None)
    except (gdrive_config.ConfigError, gdrive_config.ConfigConflict) as exc:
        raise _config_error(exc) from exc
    gdrive_client.forget_all()
    return {"key_source": source, "key_configured": True}


class ClientIn(BaseModel):
    label: str = ""
    client_id: str
    client_secret: str


class ClientPatch(BaseModel):
    label: str | None = None
    client_id: str | None = None
    client_secret: str | None = None  # empty/None = keep
    enabled: bool | None = None


@router.post("/clients")
async def create_client(payload: ClientIn):
    try:
        return await gdrive_config.create_client(
            label=payload.label, client_id=payload.client_id, client_secret=payload.client_secret
        )
    except (gdrive_config.ConfigError, gdrive_config.ConfigConflict) as exc:
        raise _config_error(exc) from exc


@router.patch("/clients/{client_pk}")
async def update_client(client_pk: int, payload: ClientPatch):
    try:
        out = await gdrive_config.update_client(
            client_pk,
            label=payload.label,
            client_id=payload.client_id,
            client_secret=payload.client_secret,
            enabled=payload.enabled,
        )
    except (gdrive_config.ConfigError, gdrive_config.ConfigConflict) as exc:
        raise _config_error(exc) from exc
    gdrive_client.forget_all()  # creds changed → mint fresh access tokens
    return out


@router.delete("/clients/{client_pk}")
async def delete_client(client_pk: int):
    try:
        await gdrive_config.delete_client(client_pk)
    except (gdrive_config.ConfigError, gdrive_config.ConfigConflict) as exc:
        raise _config_error(exc) from exc
    return {"ok": True}


class AccountPatch(BaseModel):
    label: str | None = None
    enabled: bool | None = None


@router.patch("/accounts/{account_id}")
async def update_account(account_id: int, payload: AccountPatch):
    async with SessionLocal() as session:
        acc = await session.get(GDriveAccount, account_id)
        if acc is None:
            raise HTTPException(status_code=404, detail="帳號不存在")
        if payload.label is not None:
            acc.label = payload.label.strip()[:128]
        if payload.enabled is not None:
            acc.enabled = payload.enabled
        await session.commit()
        await session.refresh(acc)
        out = gdrive_accounts.to_public(acc)
    gdrive_transfer_queue.notify()
    return out


@router.post("/accounts/{account_id}/refresh")
async def refresh_account(account_id: int):
    try:
        return await gdrive_accounts.refresh_quota(account_id)
    except gdrive_accounts.AccountUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GDriveError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: int):
    async with SessionLocal() as session:
        acc = await session.get(GDriveAccount, account_id)
        if acc is None:
            raise HTTPException(status_code=404, detail="帳號不存在")
        stored = (
            await session.execute(
                select(func.count()).select_from(GDriveLocation).where(GDriveLocation.account_id == account_id)
            )
        ).scalar_one()
        if stored:
            raise HTTPException(
                status_code=409,
                detail=f"此帳號上還有 {stored} 個已搬移的檔案紀錄,無法刪除(可改為停用)",
            )
        active = (
            await session.execute(
                select(func.count()).select_from(GDriveTransfer).where(
                    GDriveTransfer.account_id == account_id,
                    GDriveTransfer.status.in_(("pending", "running")),
                )
            )
        ).scalar_one()
        if active:
            raise HTTPException(status_code=409, detail="此帳號還有進行中的搬移,請先取消")
        await session.delete(acc)
        await session.commit()
    gdrive_client.forget(account_id)
    return {"ok": True}


# ---------- traffic policy ----------


@router.get("/policy")
async def get_policy():
    return (await gdrive_policy.load()).to_public()


class PolicyIn(BaseModel):
    daily_budget_gb: float | None = Field(default=None, ge=0)
    window: str | None = None
    paused: bool | None = None


@router.put("/policy")
async def put_policy(payload: PolicyIn):
    try:
        policy = await gdrive_policy.update(
            daily_budget_gb=payload.daily_budget_gb, window=payload.window, paused=payload.paused
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    gdrive_transfer_queue.notify()
    return policy.to_public()


# ---------- move jobs ----------


class MigrateIn(BaseModel):
    studio_ids: list[str] = Field(min_length=1)
    account_id: int
    delete_source: bool = True


@router.post("/migrate")
async def migrate(payload: MigrateIn):
    await _require_ready()
    agg = await studio_index.get()
    targets: list[tuple[str, str, str]] = []
    missing: list[dict] = []
    for sid in dict.fromkeys(s.strip() for s in payload.studio_ids if s.strip()):
        entry = agg.studios.get(sid)
        if entry is None:
            missing.append(
                {"kind": "studio", "studio_id": sid, "job_id": None, "error": "找不到此製作商的已下載作品"}
            )
        else:
            targets.append(("studio", sid, entry.name))
    try:
        results = await gdrive_transfer_queue.create_jobs(
            targets, account_id=payload.account_id, delete_source=payload.delete_source
        )
    except gdrive_accounts.AccountUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"results": missing + results}


def _job_public(job: GDriveJob, stats: dict, labels: dict[int, str]) -> dict:
    return {
        "id": job.id,
        "listing_kind": job.listing_kind,
        "origin": job.origin,
        "studio_id": job.studio_id,
        "studio_name": job.studio_name,
        "account_id": job.account_id,
        "account_label": labels.get(job.account_id, f"#{job.account_id}"),
        "status": job.status,
        "delete_source": job.delete_source,
        "source_state": job.source_state,
        "code_count": job.code_count,
        "file_count": job.file_count,
        "total_bytes": job.total_bytes,
        "uploaded_bytes": int(stats.get("uploaded", 0)),
        "counts": stats.get("counts", {}),
        "message": job.message,
        "created_at": job.created_at.isoformat() + "Z",
        "finished_at": job.finished_at.isoformat() + "Z" if job.finished_at else None,
    }


@router.get("/jobs")
async def list_jobs(limit: int = Query(100, ge=1, le=500)):
    async with SessionLocal() as session:
        jobs = (
            await session.execute(select(GDriveJob).order_by(GDriveJob.id.desc()).limit(limit))
        ).scalars().all()
        ids = [j.id for j in jobs]
        stats: dict[int, dict] = {i: {"counts": {}, "uploaded": 0} for i in ids}
        if ids:
            for job_id, st, n, up in (
                await session.execute(
                    select(
                        GDriveTransfer.job_id,
                        GDriveTransfer.status,
                        func.count(),
                        func.sum(GDriveTransfer.bytes_uploaded),
                    )
                    .where(GDriveTransfer.job_id.in_(ids))
                    .group_by(GDriveTransfer.job_id, GDriveTransfer.status)
                )
            ).all():
                stats[job_id]["counts"][st] = int(n)
                stats[job_id]["uploaded"] += int(up or 0)
        labels = {
            a.id: a.label or a.email
            for a in (await session.execute(select(GDriveAccount))).scalars()
        }
    return {
        "jobs": [_job_public(j, stats.get(j.id, {}), labels) for j in jobs],
        "queue": await gdrive_transfer_queue.status(),
    }


@router.get("/jobs/{job_id}/transfers")
async def job_transfers(job_id: int, status: str = Query("")):
    async with SessionLocal() as session:
        q = select(GDriveTransfer).where(GDriveTransfer.job_id == job_id)
        if status:
            q = q.where(GDriveTransfer.status == status)
        rows = (await session.execute(q.order_by(GDriveTransfer.id))).scalars().all()
    return [
        {
            "id": r.id,
            "code": r.code,
            "name": r.pikpak_name,
            "size": r.pikpak_size,
            "pikpak_path": r.pikpak_path,
            "drive_dir": r.drive_dir,
            "bytes_uploaded": r.bytes_uploaded,
            "status": r.status,
            "message": r.message,
            "attempts": r.attempts,
            "drive_file_id": r.drive_file_id,
        }
        for r in rows
    ]


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: int):
    return {"requeued": await gdrive_transfer_queue.retry_job(job_id)}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: int):
    return {"cancelled": await gdrive_transfer_queue.cancel_job(job_id)}


@router.post("/jobs/{job_id}/trash-source")
async def trash_job_source(job_id: int):
    ok = await gdrive_transfer_queue.trash_source(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="此工作尚未全部完成,或刪除 PikPak 原檔失敗")
    return {"ok": True}


@router.post("/transfers/{transfer_id}/retry")
async def retry_transfer(transfer_id: int):
    if not await gdrive_transfer_queue.retry(transfer_id):
        raise HTTPException(status_code=409, detail="只能重試失敗或已取消的檔案")
    return {"ok": True}


@router.post("/jobs/cleanup")
async def cleanup_jobs():
    return {"deleted": await gdrive_transfer_queue.cleanup()}


@router.get("/active-studios")
async def active_studios():
    """Studio ids with a running job — the studio page greys them out."""
    async with SessionLocal() as session:
        ids = (
            await session.execute(
                select(GDriveJob.studio_id).where(
                    GDriveJob.status.in_(ACTIVE_JOB_STATES), GDriveJob.listing_kind == "studio"
                )
            )
        ).scalars().all()
    return sorted(set(ids))


# ---------- locations ----------


@router.get("/locate/{code}")
async def locate(code: str):
    keys = {code.strip(), (normalize_code(code) or "").strip()} - {""}
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(GDriveLocation)
                .where(GDriveLocation.code.in_(keys))
                .order_by(GDriveLocation.account_id, GDriveLocation.path)
            )
        ).scalars().all()
        accounts = {
            a.id: a
            for a in (
                await session.execute(
                    select(GDriveAccount).where(GDriveAccount.id.in_({r.account_id for r in rows}))
                )
            ).scalars()
        } if rows else {}
    groups: dict[int, dict] = {}
    for r in rows:
        acc = accounts.get(r.account_id)
        g = groups.setdefault(
            r.account_id,
            {
                "account_id": r.account_id,
                "account_label": (acc.label or acc.email) if acc else f"#{r.account_id}",
                "account_email": acc.email if acc else "",
                "folder_url": folder_url(r.code_folder_id or r.drive_folder_id),
                "files": [],
            },
        )
        g["files"].append({
            "file_id": r.drive_file_id,
            "name": r.name,
            "size": r.size,
            "is_video": r.is_video,
            "path": r.path,
            "view_url": file_view_url(r.drive_file_id),
            "preview_url": file_preview_url(r.drive_file_id) if r.is_video else "",
        })
    return {"code": code, "locations": list(groups.values())}


@router.post("/locate")
async def locate_many(codes: list[str] = Body(..., embed=True)):
    """Bulk "which of these codes are on Drive" for list badges."""
    agg = await studio_index.get()
    out = []
    for c in codes[:2000]:
        if c in agg.gdrive_codes or (normalize_code(c) or "") in agg.gdrive_codes:
            out.append(c)
    return {"codes": out}
