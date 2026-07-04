"""Single-account login gate endpoints.

``/status``, ``/setup`` and ``/login`` are public (the frontend needs
them before it has a token); ``/me`` and ``/change-password`` require a
valid Bearer token. There is no logout endpoint — the token is a
stateless JWT, so the client just drops it from localStorage.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..schemas import (
    AuthStatus,
    ChangePasswordIn,
    LoginIn,
    MeOut,
    SetupIn,
    TokenOut,
)
from ..services import auth as auth_service

router = APIRouter(prefix="/api/auth", tags=["auth"])

_MIN_PASSWORD_LEN = 6


@router.get("/status", response_model=AuthStatus)
async def status(session: AsyncSession = Depends(get_session)):
    """Whether first-run setup is still needed. Lets the frontend route a
    fresh install to /setup instead of /login."""
    return AuthStatus(needs_setup=not await auth_service.is_configured(session))


@router.post("/setup", response_model=TokenOut)
async def setup(payload: SetupIn, session: AsyncSession = Depends(get_session)):
    username = payload.username.strip()
    if not username or not payload.password:
        raise HTTPException(status_code=400, detail="請填入帳號與密碼")
    if len(payload.password) < _MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400, detail=f"密碼至少 {_MIN_PASSWORD_LEN} 個字元")
    try:
        await auth_service.create_account(session, username, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="帳號已設定,請改用登入") from exc
    return TokenOut(token=auth_service.create_token(username), username=username)


@router.post("/login", response_model=TokenOut)
async def login(payload: LoginIn, session: AsyncSession = Depends(get_session)):
    locked = auth_service.login_locked_for()
    if locked > 0:
        raise HTTPException(
            status_code=429, detail=f"登入失敗次數過多,請 {int(locked) + 1} 秒後再試"
        )
    username = payload.username.strip()
    if not await auth_service.verify_login(session, username, payload.password):
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
    return TokenOut(token=auth_service.create_token(username), username=username)


@router.get("/me", response_model=MeOut)
async def me(username: str = Depends(auth_service.require_auth)):
    return MeOut(username=username)


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordIn,
    username: str = Depends(auth_service.require_auth),
    session: AsyncSession = Depends(get_session),
):
    if len(payload.new_password) < _MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400, detail=f"新密碼至少 {_MIN_PASSWORD_LEN} 個字元")
    if not await auth_service.update_password(
        session, payload.old_password, payload.new_password
    ):
        raise HTTPException(status_code=400, detail="舊密碼錯誤")
    return {"ok": True}
