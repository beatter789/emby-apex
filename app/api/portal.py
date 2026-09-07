"""Versioned portal authentication and account API."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import services, settings_store
from ..db import get_session
from ..config import get_settings
from ..models import AppSetting, RedeemCode, Server, utcnow
from ..portal_routes import _describe_status
from ..security import PORTAL_SESSION_KEY, csrf_ok
from ..services import RegistrationError
from .common import csrf_data, csrf_required, error, get_portal_user, iso_seconds, ok, payload

router = APIRouter(tags=["portal-api"])
DbSession = Annotated[AsyncSession, Depends(get_session)]
logger = logging.getLogger(__name__)


def _activation_image_path(relative: str | None) -> Path | None:
    if not relative:
        return None
    root = get_settings().image_dir.resolve()
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        return None
    return path


async def _activation_image_meta(db: AsyncSession) -> dict[str, str]:
    rows = {
        row.key: row.value
        for row in (
            await db.scalars(
                select(AppSetting).where(
                    AppSetting.key.in_(
                        ["activation_image_path", "activation_image_updated_at", "activation_image_mime"]
                    )
                )
            )
        ).all()
    }
    return rows


def _account_data(user) -> dict[str, Any]:
    return {
        "username": user.username,
        "server_id": user.server_id,
        "status": _describe_status(user),
        "is_disabled": bool(user.is_disabled),
        "playback_enabled": bool(user.playback_enabled),
        "playback_source": user.playback_source,
        "expires_at": iso_seconds(services.as_utc(user.expires_at)),
        "synced_at": iso_seconds(services.as_utc(user.synced_at)),
        "registered_at": iso_seconds(services.as_utc(user.registered_at)),
    }


@router.post("/auth/login")
async def login(request: Request, db: DbSession) -> Any:
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    user = await services.authenticate_portal_user(db, str(data.get("username") or ""), str(data.get("password") or ""))
    if user is None:
        return error("用户名或密码错误", status_code=401)
    request.session.clear()
    request.session[PORTAL_SESSION_KEY] = user.id
    return ok({"user_id": user.id, "username": user.username})


@router.post("/auth/register")
async def register(request: Request, db: DbSession) -> Any:
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    # Admin and portal listeners are separate processes. Reload the persisted
    # value so a settings change takes effect without restarting portal.
    await settings_store.load(db)
    if not settings_store.current().registration_enabled:
        return error("当前未开放注册", status_code=404)
    if str(data.get("password") or "") != str(data.get("password2") or data.get("password_confirm") or ""):
        return error("两次输入的密码不一致", status_code=400)
    try:
        user = await services.register_user(db, username=str(data.get("username") or ""), password=str(data.get("password") or ""))
    except RegistrationError as exc:
        return error(str(exc), status_code=400)
    request.session.clear()
    request.session[PORTAL_SESSION_KEY] = user.id
    return ok({"user_id": user.id, "username": user.username, "claimed": user.claimed_at is not None}, status_code=201)


@router.post("/auth/logout")
async def logout(request: Request) -> Any:
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    request.session.clear()
    return ok(None)


@router.get("/account")
async def account(request: Request, db: DbSession) -> Any:
    user = await get_portal_user(request, db)
    if user is None:
        return error("未登录或登录已失效", status_code=401)
    server = await db.get(Server, user.server_id)
    data = _account_data(user)
    data["server_name"] = server.name if server else ""
    data["csrf_token"] = csrf_data(request)["csrf_token"]
    return ok(data)


@router.get("/account/redeem-codes")
async def redeem_codes(request: Request, db: DbSession) -> Any:
    """Return only redemption records belonging to the current portal user."""
    user = await get_portal_user(request, db)
    if user is None:
        return error("未登录或登录已失效", status_code=401)
    rows = (
        await db.scalars(
            select(RedeemCode)
            .where(RedeemCode.used_by_user_id == user.id)
            .order_by(RedeemCode.used_at.desc())
            .limit(200)
        )
    ).all()
    return ok(
        {
            "codes": [
                {
                    "code": row.code,
                    "duration": services.describe_duration(row.amount, row.unit),
                    "amount": row.amount,
                    "unit": row.unit,
                    "used_at": iso_seconds(row.used_at),
                }
                for row in rows
            ]
        }
    )


@router.get("/account/activation")
async def activation(request: Request, db: DbSession) -> Any:
    """Return only this user's pending bill-code-generated redeem codes."""
    user = await get_portal_user(request, db)
    if user is None:
        return error("未登录或登录已失效", status_code=401)
    try:
        now = utcnow()
        rows = (
            await db.scalars(
                select(RedeemCode)
                .where(
                    RedeemCode.owner_user_id == user.id,
                    RedeemCode.used_at.is_(None),
                    (RedeemCode.expires_at.is_(None) | (RedeemCode.expires_at > now)),
                )
                .order_by(RedeemCode.created_at.desc())
                .limit(200)
            )
        ).all()
        used_today = await services.bill_code_usage_today(db, user.id)
        meta = await _activation_image_meta(db)
    except Exception:
        logger.exception("portal 激活数据读取失败")
        return error("激活数据暂时不可用，请稍后重试", status_code=500)
    return ok(
        {
            "pending_codes": [
                {
                    "code": row.code,
                    "generated_at": iso_seconds(row.created_at),
                    "expires_at": iso_seconds(row.expires_at),
                }
                for row in rows
            ],
            "used_today": used_today,
            "remaining_today": max(0, services.BILL_CODE_DAILY_LIMIT - used_today),
            "scan_image_url": "/api/v1/account/activation-image" if meta.get("activation_image_path") else "",
            "image_updated_at": meta.get("activation_image_updated_at"),
        }
    )


@router.get("/account/activation-image")
async def activation_image(request: Request, db: DbSession) -> Response:
    user = await get_portal_user(request, db)
    if user is None:
        return error("未登录或登录已失效", status_code=401)
    try:
        meta = await _activation_image_meta(db)
    except Exception:
        logger.exception("portal 激活图片元数据读取失败")
        return error("扫码图片暂时不可用，请稍后重试", status_code=500)
    path = _activation_image_path(meta.get("activation_image_path"))
    if path is None:
        return error("扫码图片不存在", status_code=404)
    return FileResponse(
        path,
        media_type=meta.get("activation_image_mime") or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.get("/account/status")
async def account_status(request: Request, db: DbSession) -> Any:
    user = await get_portal_user(request, db)
    if user is None:
        return error("未登录或登录已失效", status_code=401)
    try:
        await services.refresh_user_from_emby(db, user)
    except Exception:
        logger.exception("portal 账户状态刷新失败")
        return error("刷新失败，请稍后重试", status_code=503)
    return ok(_account_data(user))


@router.post("/account/redeem")
async def redeem(request: Request, db: DbSession) -> Any:
    user = await get_portal_user(request, db)
    if user is None:
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    try:
        used = await services.redeem_code(db, user, str(data.get("code") or ""))
    except RegistrationError as exc:
        return error(str(exc), status_code=400)
    return ok({"duration": services.describe_duration(used.amount, used.unit), "account": _account_data(user)})


@router.post("/account/activation/bill-code")
async def activation_bill_code(request: Request, db: DbSession) -> Any:
    user = await get_portal_user(request, db)
    if user is None:
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    try:
        redeem, used_today = await services.create_redeem_code_from_bill(
            db, user, str(data.get("bill_code") or data.get("code") or "")
        )
    except RegistrationError as exc:
        return error(str(exc), status_code=400)
    except Exception as exc:
        # Never let a provider/database exception echo a submitted bill code
        # or validation detail through the API response.
        logger.warning("portal 账单码处理失败 error_type=%s", type(exc).__name__)
        return error("账单码暂时不可用，请稍后重试", status_code=503)
    return ok(
        {
            "redeem_code": redeem.code,
            "generated_at": iso_seconds(redeem.created_at),
            "expires_at": iso_seconds(redeem.expires_at),
            "used_today": used_today,
            "remaining_today": max(0, services.BILL_CODE_DAILY_LIMIT - used_today),
        },
        status_code=201,
    )


@router.post("/account/password")
async def change_password(request: Request, db: DbSession) -> Any:
    user = await get_portal_user(request, db)
    if user is None:
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    if str(data.get("new_password") or "") != str(data.get("new_password2") or data.get("password_confirm") or ""):
        return error("两次输入的新密码不一致", status_code=400)
    try:
        await services.change_portal_password(db, user, str(data.get("old_password") or ""), str(data.get("new_password") or ""))
    except RegistrationError as exc:
        return error(str(exc), status_code=400)
    return ok({"message": "密码已修改"})


@router.post("/account/username")
async def change_username(request: Request, db: DbSession) -> Any:
    user = await get_portal_user(request, db)
    if user is None:
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    try:
        await services.change_portal_username(db, user, str(data.get("new_username") or ""))
    except RegistrationError as exc:
        return error(str(exc), status_code=400)
    return ok({"account": _account_data(user)})
