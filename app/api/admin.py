"""Versioned administrator API used by the Vue management application."""

from __future__ import annotations

from datetime import timedelta
import logging
import re
import time
from pathlib import Path
from typing import Any, Annotated

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import notify, scheduler, services, settings_store, stats
from ..config import get_settings as app_settings
from ..db import get_session
from ..emby import EmbyClient, EmbyError
from ..models import ActionLog, AppSetting, ManagedUser, MediaRequest, RedeemCode, Server
from ..routes import _parse_local_input, _safe_server_url, _redact_sensitive_text
from ..security import encrypt_secret, is_logged_in, verify_admin
from ..tmdb import TmdbClient, TmdbError
from ..moviepilot import MoviePilotClient, MoviePilotError
from ..wecom import WeComConfig, WeComError
from .common import (
    csrf_required,
    error,
    format_policy_detail,
    iso_seconds,
    ok,
    payload,
)

router = APIRouter(tags=["admin-api"])
DbSession = Annotated[AsyncSession, Depends(get_session)]
logger = logging.getLogger(__name__)

_ACTIVATION_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_ACTIVATION_IMAGE_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


async def _activation_image_settings(db: AsyncSession) -> dict[str, str]:
    return {
        row.key: row.value
        for row in (
            await db.scalars(
                select(AppSetting).where(
                    AppSetting.key.in_(
                        ["activation_image_path", "activation_image_updated_at", "activation_image_mime", "activation_image_size"]
                    )
                )
            )
        ).all()
    }


def _admin_required(request: Request) -> bool:
    return is_logged_in(request)


def _bool_value(value: Any, default: bool = False) -> bool:
    """Normalize JSON booleans and HTML form checkbox strings alike."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _server_row(server: Server) -> dict[str, Any]:
    return {
        "id": server.id,
        "name": server.name,
        "base_url": _safe_server_url(server.base_url),
        "enabled": bool(server.enabled),
        "status": "paused" if not server.enabled else "error" if server.last_error else "ok",
        "has_error": bool(server.last_error),
        "server_version": server.server_version,
        "last_ok_at": iso_seconds(server.last_ok_at),
        "is_register_target": bool(server.is_register_target),
    }


def _user_row(
    user: ManagedUser,
    server_name: str,
    playing: bool,
) -> dict[str, Any]:
    return {
        "id": user.id,
        "server_id": user.server_id,
        "server_name": server_name,
        "emby_user_id": user.emby_user_id,
        "username": user.username,
        "is_disabled": bool(user.is_disabled),
        "is_admin": bool(user.is_admin),
        "is_protected": bool(user.is_protected),
        "portal_enabled": bool(user.portal_enabled),
        "portal_password_configured": bool(user.portal_password_hash),
        "is_activated": user.is_activated,
        "activation_failed_attempts": int(user.activation_failed_attempts or 0),
        "activation_locked": bool(user.activation_locked),
        "activation_locked_at": iso_seconds(services.as_utc(user.activation_locked_at)),
        "playback_enabled": bool(user.playback_enabled),
        "playback_source": user.playback_source,
        # API timestamps use one ISO representation at second precision.
        "expires_at": iso_seconds(services.as_utc(user.expires_at)),
        "registered_at": iso_seconds(services.as_utc(user.registered_at)),
        "synced_at": iso_seconds(services.as_utc(user.synced_at)),
        "is_playing": playing,
        "total_playback_seconds": float(user.total_playback_seconds or 0),
        "playback_hours": round((user.total_playback_seconds or 0) / 3600, 1),
        "last_played_at": iso_seconds(services.as_utc(user.last_played_at)),
        "policy": services.policy_for_user(user),
        "client_policy_mode": user.client_policy_mode,
        "client_patterns": user.client_patterns,
        "note": user.note,
    }


def _request_group_row(group: dict[str, Any]) -> dict[str, Any]:
    """Serialize a grouped request without leaking ORM internals."""
    items = []
    for entry in group.get("items", []):
        row = entry.get("request")
        if row is None:
            continue
        items.append(
            {
                "id": row.id,
                "username": entry.get("username") or "",
                "note": row.note,
                "status": row.status,
                "created_at": iso_seconds(row.created_at),
                "rejection_reason": row.rejection_reason,
            }
        )
    poster_id = items[0]["id"] if items else None
    return {
        "server_id": group["server_id"],
        "server_name": group.get("server_name") or "",
        "tmdb_id": group["tmdb_id"],
        "media_type": group["media_type"],
        "title": group.get("title") or "",
        "original_title": group.get("original_title") or "",
        "year": group.get("year"),
        "overview": group.get("overview") or "",
        "poster_url": group.get("poster_url") or "",
        "poster_local_url": f"/api/v1/requests/{poster_id}/poster" if group.get("poster_local_path") and poster_id else "",
        "poster_error": group.get("poster_error") or "",
        "status": group.get("status") or "pending",
        "items": items,
    }


_NON_SENSITIVE_SETTING_NAMES = frozenset(
    {
        "allowembyselfpassword",
        "portalpasswordminlength",
        # The administrator explicitly requested a plain-text TMDB key field
        # in the settings form.  It remains admin-session-only and is never
        # included in portal payloads or connection-test responses.
        "tmdbapikey",
    }
)
_SENSITIVE_SETTING_MARKERS = (
    "secret",
    "token",
    "password",
    "passwd",
    "apikey",
    "privatekey",
    "encodingaeskey",
)


def _normalized_setting_name(name: str) -> str:
    """Normalize setting names before checking credential markers."""
    return re.sub(r"[^a-z0-9]", "", str(name).casefold())


def _is_sensitive_setting(name: str, kind: str | None = None) -> bool:
    """Identify credentials without treating ordinary password-related options as secrets."""
    normalized = _normalized_setting_name(name)
    if kind == "secret":
        return True
    if normalized in _NON_SENSITIVE_SETTING_NAMES:
        return False
    return any(marker in normalized for marker in _SENSITIVE_SETTING_MARKERS)


def _public_settings() -> dict[str, Any]:
    """Expose runtime toggles without serializing credentials or tokens."""
    runtime = settings_store.current()
    values = {
        name: value
        for name, value in vars(runtime).items()
        if not _is_sensitive_setting(
            name,
            settings_store.FIELD_SPECS.get(name, ("", None, None, "", ""))[0],
        )
    }
    # A persisted toggle is only effective when the corresponding channel is
    # fully configured. This keeps old rows with missing credentials from
    # appearing as enabled in the settings API.
    for category in ("registration", "expiry", "media_request", "activation", "general"):
        for channel in ("webhook", "telegram", "wecom"):
            name = f"notify_{category}_{channel}"
            if name in values:
                values[name] = settings_store.notification_enabled(runtime, category, channel)
    grouped: dict[str, dict[str, Any]] = {}
    prefixes = {
        "notify_registration_": "registration",
        "notify_expiry_": "expiry",
        "notify_media_request_": "media_request",
        "notify_activation_": "activation",
        "notify_general_": "general",
    }
    for name in list(values):
        for prefix, group in prefixes.items():
            if name.startswith(prefix):
                grouped.setdefault(group, {})[name[len(prefix):]] = values.pop(name)
                break
    values.update(grouped)
    return values


_SETTING_PREFIXES = {
    "registration.": "notify_registration_",
    "expiry.": "notify_expiry_",
    "media_request.": "notify_media_request_",
    "activation.": "notify_activation_",
    "general.": "notify_general_",
}


def _flatten_setting_updates(value: Any, prefix: str = "") -> dict[str, str]:
    """Accept concise nested settings while retaining the DB's flat keys."""
    if not isinstance(value, dict):
        return {prefix: str(value)} if prefix else {}
    result: dict[str, str] = {}
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, dict):
            result.update(_flatten_setting_updates(item, name))
        else:
            result[name] = str(item)
    return result


def _expand_setting_name(name: str) -> str:
    for prefix, flat_prefix in _SETTING_PREFIXES.items():
        if name.startswith(prefix):
            return flat_prefix + name[len(prefix):]
    return name


def _setting_rows() -> list[dict[str, Any]]:
    rows = []
    for row in settings_store.form_rows():
        public_name = row["name"]
        for prefix, flat_prefix in _SETTING_PREFIXES.items():
            if public_name.startswith(flat_prefix):
                public_name = prefix + public_name[len(flat_prefix):]
                break
        row = {**row, "name": public_name}
        if _is_sensitive_setting(row["name"], row.get("kind")):
            row = {
                **row,
                "value": "",
                "configured": bool(row.get("configured") or row.get("value")),
                "sensitive": True,
            }
        rows.append(row)
    return rows


async def _registration_settings(db: AsyncSession) -> dict[str, Any]:
    """Return the registration target and selectable enabled servers."""
    target = await services.get_register_target(db)
    servers = (
        await db.scalars(
            select(Server).where(Server.enabled.is_(True)).order_by(Server.name)
        )
    ).all()
    return {
        "registration_server_id": target.id if target else None,
        "registration_servers": [
            {"id": server.id, "name": server.name, "enabled": bool(server.enabled)}
            for server in servers
        ],
    }


@router.post("/auth/login")
async def login(request: Request, db: DbSession) -> Any:
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    username = str(data.get("username") or "")
    password = str(data.get("password") or "")
    if not verify_admin(username, password):
        return error("用户名或密码错误", status_code=401)
    request.session.clear()
    request.session["admin"] = username
    return ok({"username": username})


@router.post("/auth/logout")
async def logout(request: Request) -> Any:
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    request.session.clear()
    return ok(None)


@router.get("/servers")
async def servers(request: Request, db: DbSession) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    rows = (await db.scalars(select(Server).order_by(Server.name))).all()
    return ok({"servers": [_server_row(row) for row in rows]})


@router.post("/servers")
async def add_server(request: Request, db: DbSession) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    name = str(data.get("name") or "").strip()
    base_url = str(data.get("base_url") or "").strip().rstrip("/")
    api_key = str(data.get("api_key") or "").strip()
    if not name or not api_key or not base_url.startswith(("http://", "https://")):
        return error("服务器名称、地址和 API Key 均不能为空", status_code=400)
    probe = EmbyClient(base_url, api_key, verify_ssl=bool(data.get("verify_ssl")), timeout=12)
    try:
        async with probe as client:
            info = await client.system_info()
    except (EmbyError, ValueError):
        return error("服务器连接失败，请检查地址和 API Key", status_code=400)
    server = Server(
        name=name,
        base_url=base_url,
        api_key_encrypted=encrypt_secret(api_key),
        verify_ssl=bool(data.get("verify_ssl")),
        server_version=info.get("Version"),
        last_ok_at=services.utcnow(),
    )
    db.add(server)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        return error("同名服务器已存在或无法保存", status_code=409)
    await services.log_action(db, "server_added", f"新增服务器 {base_url}", server_name=name)
    return ok(_server_row(server), status_code=201)


@router.patch("/servers/{server_id}")
async def update_server(server_id: int, request: Request, db: DbSession) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    server = await db.get(Server, server_id)
    if server is None:
        return error("服务器不存在", status_code=404)
    if "enabled" in data:
        server.enabled = bool(data["enabled"])
    if "verify_ssl" in data:
        server.verify_ssl = bool(data["verify_ssl"])
    await services.log_action(db, "server_updated", "更新服务器设置", server_name=server.name)
    return ok(_server_row(server))


@router.post("/servers/{server_id}/sync")
async def sync_server(server_id: int, request: Request, db: DbSession) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    server = await db.get(Server, server_id)
    if server is None:
        return error("服务器不存在", status_code=404)
    try:
        count = await services.sync_server_users(db, server)
    except (EmbyError, ValueError):
        return error("同步失败，请检查服务器连接", status_code=502)
    return ok({"server_id": server_id, "count": count})


@router.delete("/servers/{server_id}")
async def delete_server(server_id: int, request: Request, db: DbSession) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    server = await db.get(Server, server_id)
    if server is None:
        return error("服务器不存在", status_code=404)
    name = server.name
    await db.delete(server)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        return error("同名服务器已存在或无法保存", status_code=409)
    await services.log_action(db, "server_deleted", f"删除服务器 {name}（Emby 侧数据不受影响）")
    return ok({"server_id": server_id})


@router.get("/users")
async def users(request: Request, db: DbSession, server_id: int | None = None, q: str = "") -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    servers_rows = (await db.scalars(select(Server).order_by(Server.name))).all()
    server_names = {server.id: server.name for server in servers_rows}
    query = select(ManagedUser).order_by(ManagedUser.username)
    if server_id is not None:
        query = query.where(ManagedUser.server_id == server_id)
    if q.strip():
        query = query.where(ManagedUser.username.ilike(f"%{q.strip()}%"))
    rows = (await db.scalars(query)).all()
    playing = {(item.server_id, item.emby_user_id) for item in scheduler.live_cache}
    return ok({
        "users": [
            _user_row(
                row,
                server_names.get(row.server_id, "?"),
                (row.server_id, row.emby_user_id) in playing,
            )
            for row in rows
        ],
        "servers": [_server_row(row) for row in servers_rows],
    })


@router.post("/users")
async def create_user(request: Request, db: DbSession) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    try:
        server = await db.get(Server, int(data.get("server_id") or 0))
    except (TypeError, ValueError):
        server = None
    if server is None:
        return error("服务器不存在", status_code=404)
    try:
        ordinary_registration = _bool_value(data.get("ordinary_registration"))
        expires_at = None if ordinary_registration else _parse_local_input(str(data.get("expires_at") or ""))
        allow_playback = _bool_value(data.get("allow_playback"), not ordinary_registration)
        user = await services.create_managed_user(
            db,
            server=server,
            username=str(data.get("username") or ""),
            password=str(data.get("password") or ""),
            ordinary_registration=ordinary_registration,
            allow_playback=allow_playback,
            expires_at=expires_at,
        )
    except (services.RegistrationError, ValueError):
        return error("用户创建失败，请检查输入或服务器连接", status_code=400)
    return ok(_user_row(user, server.name, False), status_code=201)


@router.post("/users/status")
async def refresh_users(request: Request, db: DbSession) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    result = await services.sync_all_servers(db)
    return ok({"servers": result})


@router.post("/users/batch")
async def batch_users(request: Request, db: DbSession) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    action = str(data.get("action") or "")
    raw_ids = data.get("user_ids") or []
    if not isinstance(raw_ids, list):
        raw_ids = [raw_ids]
    try:
        ids = [int(value) for value in raw_ids]
    except (TypeError, ValueError):
        ids = []
    if action not in {"enable", "disable", "playback_enable", "playback_disable"}:
        return error("未知批量操作", status_code=400)
    rows = (await db.scalars(select(ManagedUser).where(ManagedUser.id.in_(ids)))).all() if ids else []
    results: list[dict[str, Any]] = []
    for user in rows:
        if user.is_admin:
            results.append({"id": user.id, "ok": False, "message": "管理员账号已跳过"})
            continue
        try:
            if action in {"enable", "disable"}:
                await services.set_user_state(db, user, disabled=action == "disable", by_controller=action == "disable", reason="管理员批量操作")
            else:
                await services.update_user_policy(db, user, {"EnableMediaPlayback": action == "playback_enable"})
            results.append({"id": user.id, "ok": True, "username": user.username})
        except (EmbyError, ValueError, services.RegistrationError):
            results.append({"id": user.id, "ok": False, "username": user.username, "message": "操作失败"})
    return ok({"results": results, "success": sum(1 for row in results if row["ok"]), "total": len(results)})


@router.get("/users/{user_id}")
async def user_detail(user_id: int, request: Request, db: DbSession) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    user = await db.get(ManagedUser, user_id)
    if user is None:
        return error("用户不存在", status_code=404)
    server = await db.get(Server, user.server_id)
    playing = any(item.server_id == user.server_id and item.emby_user_id == user.emby_user_id for item in scheduler.live_cache)
    return ok(_user_row(user, server.name if server else "?", playing))


@router.patch("/users/{user_id}")
async def update_user(user_id: int, request: Request, db: DbSession) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    user = await db.get(ManagedUser, user_id)
    if user is None:
        return error("用户不存在", status_code=404)
    if "portal_enabled" in data and user.is_admin:
        if _bool_value(data.get("portal_enabled")) != bool(user.portal_enabled):
            return error("管理员账号不允许切换用户端登录", status_code=400)
    try:
        if "disabled" in data:
            if _bool_value(data["disabled"]) and user.is_admin:
                return error("管理员账号不允许停用", status_code=400)
            disabled = _bool_value(data["disabled"])
            await services.set_user_state(db, user, disabled=disabled, by_controller=disabled, reason="管理员 API 操作")
        if "expires_at" in data:
            user.expires_at = _parse_local_input(data.get("expires_at"))
        if "note" in data:
            user.note = str(data.get("note") or "").strip()
        if "playback_enabled" in data:
            await services.update_user_policy(db, user, {"EnableMediaPlayback": _bool_value(data["playback_enabled"])})
        if "client_policy_mode" in data:
            mode = str(data["client_policy_mode"])
            if mode not in {"off", "allow", "block"}:
                return error("未知的客户端限制模式", status_code=400)
            user.client_policy_mode = mode
            user.client_patterns = [str(value).strip() for value in (data.get("client_patterns") or []) if str(value).strip()]
        if isinstance(data.get("policy"), dict):
            await services.update_user_policy(db, user, data["policy"])
        if "portal_enabled" in data:
            if _bool_value(data["portal_enabled"]):
                portal_password = str(data.get("password") or "")
                # Keeping an already enabled portal login checked should be a
                # no-op; a password is only required when actually opening a
                # login that has no configured credential.
                if not user.portal_enabled or portal_password:
                    await services.enable_portal_login(db, user, portal_password)
            else:
                if user.portal_enabled:
                    await services.disable_portal_login(db, user)
        if "protected" in data:
            await services.set_user_protected(db, user, _bool_value(data["protected"]))
    except ValueError as exc:
        if str(exc) == "到期时间格式不正确":
            return error(str(exc), status_code=400)
        return error("用户更新失败，请检查输入或服务器连接", status_code=400)
    except (services.RegistrationError, EmbyError):
        return error("用户更新失败，请检查输入或服务器连接", status_code=400)
    # Policy helpers commit their own remote snapshot, but ordinary managed
    # user fields (expiry, note and client restrictions) must also survive the
    # request boundary when no helper was called.
    await db.commit()
    server = await db.get(Server, user.server_id)
    playing = any(item.server_id == user.server_id and item.emby_user_id == user.emby_user_id for item in scheduler.live_cache)
    return ok(_user_row(user, server.name if server else "?", playing))


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, request: Request, db: DbSession) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    user = await db.get(ManagedUser, user_id)
    if user is None:
        return error("用户不存在", status_code=404)
    try:
        username = await services.admin_delete_user(db, user)
    except services.RegistrationError as exc:
        return error(str(exc), status_code=400)
    return ok({"user_id": user_id, "username": username})


@router.post("/sessions/{server_id}/{session_id}/stop")
async def stop_session(server_id: int, session_id: str, request: Request, db: DbSession) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    server = await db.get(Server, server_id)
    if server is None:
        return error("服务器不存在", status_code=404)
    try:
        async with services.client_for(server) as client:
            await client.stop_playback(session_id)
    except (EmbyError, ValueError):
        return error("停止播放失败", status_code=502)
    await services.log_action(db, "session_stopped", "管理员手动停止播放", server_name=server.name)
    return ok({"server_id": server_id, "session_id": session_id})


@router.get("/requests")
async def media_requests(request: Request, db: DbSession, state: str = "pending") -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    groups = await services.admin_media_request_groups(db, state)
    return ok({"state": state if state in {"pending", "in_library", "rejected", "all"} else "pending", "requests": [_request_group_row(group) for group in groups]})


@router.get("/requests/tmdb/{media_type}/{tmdb_id}")
async def request_tmdb_details_api(
    request: Request,
    media_type: str,
    tmdb_id: str,
    db: DbSession,
) -> Any:
    """Return full TMDB details for the admin request workflow."""
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    try:
        parsed_tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        return error("TMDB ID 必须是正整数", status_code=400)
    if parsed_tmdb_id <= 0 or media_type not in services.MEDIA_TYPES:
        return error("TMDB ID 或媒体类型无效", status_code=400)
    await settings_store.load(db)
    try:
        detail = await services.tmdb_details(media_type, parsed_tmdb_id)
    except services.RegistrationError as exc:
        return error(str(exc), status_code=400)
    return ok(detail)


@router.post("/requests/{server_id}/{media_type}/{tmdb_id}/confirm")
async def confirm_media_request(
    server_id: int,
    media_type: str,
    tmdb_id: int,
    request: Request,
    db: DbSession,
) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    try:
        count = await services.confirm_media_request_group(
            db,
            server_id=server_id,
            media_type=media_type,
            tmdb_id=tmdb_id,
            confirmed_by=str(request.session.get("admin") or "admin"),
        )
    except services.RegistrationError as exc:
        return error(str(exc), status_code=400)
    return ok({"server_id": server_id, "media_type": media_type, "tmdb_id": tmdb_id, "updated": count})


@router.post("/requests/{server_id}/{media_type}/{tmdb_id}/reject")
async def reject_media_request(
    server_id: int,
    media_type: str,
    tmdb_id: int,
    request: Request,
    db: DbSession,
) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    try:
        count = await services.reject_media_request_group(
            db,
            server_id=server_id,
            media_type=media_type,
            tmdb_id=tmdb_id,
            rejected_by=str(request.session.get("admin") or "admin"),
            reason=str(data.get("reason") or ""),
        )
    except services.RegistrationError as exc:
        return error(str(exc), status_code=400)
    return ok({"server_id": server_id, "media_type": media_type, "tmdb_id": tmdb_id, "updated": count})


@router.get("/requests/{request_id}/poster")
async def media_request_poster(request_id: int, request: Request, db: DbSession) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    item = await db.get(MediaRequest, request_id)
    if item is None or not item.poster_local_path:
        return Response(status_code=404)
    root = app_settings().image_dir.resolve()
    path = (root / item.poster_local_path).resolve()
    if root not in path.parents or not path.is_file():
        return Response(status_code=404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/images/{server_id}/{item_id}")
async def item_image(server_id: int, item_id: str, request: Request, db: DbSession) -> Any:
    """Proxy an Emby image so API keys never reach the browser."""
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    server = await db.get(Server, server_id)
    if server is None:
        return Response(status_code=404)
    try:
        async with services.client_for(server) as client:
            result = await client.image_bytes(item_id)
    except (EmbyError, ValueError):
        result = None
    if result is None:
        return Response(status_code=404)
    content, content_type = result
    return Response(content=content, media_type=content_type, headers={"Cache-Control": "private, max-age=3600"})


@router.get("/history")
async def history(request: Request, db: DbSession, days: str = "30") -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    try:
        span = max(1, min(int(days), 365))
        records = await stats.recent_records(db, limit=200)
        cutoff = services.utcnow() - timedelta(days=span)
        serialized = []
        for row in records or []:
            started_at = services.as_utc(row.started_at)
            if started_at is None or started_at < cutoff:
                continue
            serialized.append(
                {
                    "id": row.id,
                    "username": row.username,
                    "item_name": row.item_name,
                    "series_name": row.series_name,
                    "client": row.client,
                    "started_at": iso_seconds(started_at),
                    "ended_at": iso_seconds(services.as_utc(row.ended_at)),
                    "watched_seconds": row.watched_seconds,
                }
            )
        return ok({"days": span, "records": serialized})
    except Exception:
        logger.exception("管理端历史 API 生成失败")
        return error("播放历史暂时不可用，请稍后重试", status_code=500)


@router.get("/logs")
async def logs(
    request: Request,
    db: DbSession,
    level: str = "",
    page: int = 1,
) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    selected_level = str(level or "").strip().lower()
    if selected_level in {"all", "全部"}:
        selected_level = ""
    if selected_level and selected_level not in {"debug", "info", "warning", "error"}:
        return error("日志级别无效", status_code=400)
    page_number = max(1, int(page or 1))
    filters = []
    if selected_level:
        filters.append(ActionLog.level == selected_level)
    total = int(await db.scalar(select(func.count(ActionLog.id)).where(*filters)) or 0)
    rows = (
        await db.scalars(
            select(ActionLog)
            .where(*filters)
            .order_by(ActionLog.created_at.desc())
            .offset((page_number - 1) * 10)
            .limit(10)
        )
    ).all()
    return ok(
        {
            "logs": [
                {
                    "id": row.id,
                    "created_at": iso_seconds(row.created_at),
                    "level": row.level,
                    "action": row.action,
                    "server_name": row.server_name,
                    "username": row.username,
                    "detail": format_policy_detail(row.detail),
                }
                for row in rows
            ],
            "level": selected_level or "all",
            "page": page_number,
            "page_size": 10,
            "total": total,
            "has_next": page_number * 10 < total,
        }
    )


@router.get("/codes")
async def codes(request: Request, db: DbSession, state: str = "all") -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    stmt = select(RedeemCode).order_by(RedeemCode.created_at.desc()).limit(400)
    if state == "unused":
        stmt = stmt.where(RedeemCode.used_at.is_(None))
    elif state == "used":
        stmt = stmt.where(RedeemCode.used_at.is_not(None))
    rows = (await db.scalars(stmt)).all()
    return ok(
        {
            "codes": [
                {
                    "id": row.id,
                    "code": row.code,
                    "amount": row.amount,
                    "unit": row.unit,
                    "note": row.note,
                    "created_at": iso_seconds(row.created_at),
                    "expires_at": iso_seconds(row.expires_at),
                    "used": row.is_used,
                    "used_by_username": row.used_by_username,
                    "used_at": iso_seconds(row.used_at),
                    # 管理端明确区分后台管理员生成码与账单码生成的用户码。
                    "source": "user" if row.source_bill_code else "admin",
                    "owner_user_id": row.owner_user_id,
                }
                for row in rows
            ],
            "unused_count": sum(1 for row in rows if not row.is_used),
        }
    )


@router.post("/codes")
async def generate_codes(request: Request, db: DbSession) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    try:
        codes = await services.generate_codes(db, amount=int(data.get("amount") or 0), unit=str(data.get("unit") or ""), quantity=int(data.get("quantity") or 1), note=str(data.get("note") or ""))
    except (ValueError, services.RegistrationError):
        return error("兑换码参数无效", status_code=400)
    return ok({"codes": [row.code for row in codes]}, status_code=201)


@router.delete("/codes/{code_id}")
async def delete_code(code_id: int, request: Request, db: DbSession) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    row = await db.get(RedeemCode, code_id)
    if row is None:
        return error("兑换码不存在", status_code=404)
    if row.is_used:
        return error("已兑换的码不能删除", status_code=400)
    if row.source_bill_code:
        # Retain the bill-code usage ledger so the same bill code can never
        # be submitted again, even if its generated redeem code is pending.
        return error("账单码生成的兑换码不能删除", status_code=400)
    await db.delete(row)
    await db.commit()
    return ok({"code_id": code_id})


@router.patch("/settings")
async def update_settings(request: Request, db: DbSession) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    raw_updates: dict[str, Any] = {
        str(key): value for key, value in data.items() if key != "csrf_token"
    }
    # A client may send either the flat short paths or a nested object under
    # ``settings``.  Both are normalized to the existing storage keys.
    if isinstance(raw_updates.get("settings"), dict):
        raw_updates = raw_updates["settings"]
    updates = {
        _expand_setting_name(name): value
        for name, value in _flatten_setting_updates(raw_updates).items()
    }
    # The registration target belongs to Server, not RuntimeSettings. Validate
    # it before saving runtime fields to avoid partially applying a bad request.
    registration_target = updates.pop("registration_server_id", None)
    target_id: int | None = None
    if registration_target not in (None, "", "null", "None"):
        try:
            target_id = int(str(registration_target))
        except (TypeError, ValueError):
            return error("默认注册服务器无效", status_code=400)
        target_server = await db.get(Server, target_id)
        if target_server is None or not target_server.enabled:
            return error("默认注册服务器不存在或已暂停", status_code=400)
    try:
        await settings_store.save(db, updates)
    except ValueError as exc:
        return error(str(exc), status_code=400)
    if registration_target is not None:
        await services.set_register_target(db, target_id)
    scheduler.reschedule()
    return ok({"settings": _public_settings(), "fields": _setting_rows(), **await _registration_settings(db)})


@router.get("/settings")
async def get_settings(request: Request, db: DbSession) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    await settings_store.load(db)
    return ok({"settings": _public_settings(), "fields": _setting_rows(), **await _registration_settings(db)})


@router.get("/settings/activation-image")
async def get_activation_image(request: Request, db: DbSession) -> Any:
    """管理员查看扫码图片元数据；实际图片由受保护 file 路由返回。"""
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    try:
        meta = await _activation_image_settings(db)
    except Exception:
        logger.exception("管理端扫码图片元数据读取失败")
        return error("扫码图片暂时不可用，请稍后重试", status_code=500)
    return ok(
        {
            "scan_image_url": "/api/v1/settings/activation-image/file" if meta.get("activation_image_path") else "",
            "image_updated_at": meta.get("activation_image_updated_at"),
            "mime": meta.get("activation_image_mime"),
            "size": int(meta.get("activation_image_size") or 0),
            "target_width": 828,
            "target_height": 1124,
        }
    )


@router.get("/settings/activation-image/file")
async def get_activation_image_file(request: Request, db: DbSession) -> Response:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    try:
        meta = await _activation_image_settings(db)
    except Exception:
        logger.exception("管理端扫码图片读取失败")
        return error("扫码图片暂时不可用，请稍后重试", status_code=500)
    relative = meta.get("activation_image_path") or ""
    root = app_settings().image_dir.resolve()
    path = (root / relative).resolve() if relative else None
    if path is None or root not in path.parents or not path.is_file():
        return error("扫码图片不存在", status_code=404)
    return FileResponse(path, media_type=meta.get("activation_image_mime") or "application/octet-stream", headers={"Cache-Control": "private, max-age=300"})


@router.post("/settings/activation-image")
async def upload_activation_image(
    request: Request, db: DbSession, file: UploadFile | None = File(default=None)
) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    # Multipart fields are not handled by ``payload`` because it would turn
    # UploadFile into an opaque string.  Header CSRF is supported as usual.
    try:
        form = await request.form()
    except Exception:
        return error("图片表单无效", status_code=400)
    submitted_csrf = str(form.get("csrf_token") or "")
    if not csrf_required(request, submitted_csrf):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    if file is None:
        return error("请上传图片文件", status_code=400)
    mime = (file.content_type or "").lower().split(";", 1)[0].strip()
    ext = _ACTIVATION_IMAGE_MIME_EXT.get(mime)
    if ext is None:
        return error("只允许 PNG、JPEG、WEBP 或 GIF 图片", status_code=400)
    original_filename = file.filename or ""
    if (
        not original_filename
        or "/" in original_filename
        or "\\" in original_filename
        or any(ord(char) < 32 for char in original_filename)
    ):
        return error("图片文件名不能包含路径", status_code=400)
    filename_ext = Path(original_filename).suffix.lower()
    safe_extensions = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    if filename_ext not in safe_extensions:
        return error("图片扩展名不安全或不受支持", status_code=400)
    if (filename_ext in {".jpg", ".jpeg"} and ext != "jpg") or (
        filename_ext not in {".jpg", ".jpeg"} and filename_ext.lstrip(".") != ext
    ):
        return error("图片扩展名与 MIME 类型不匹配", status_code=400)
    try:
        content = await file.read(_ACTIVATION_IMAGE_MAX_BYTES + 1)
    except Exception:
        return error("图片读取失败", status_code=400)
    if len(content) == 0 or len(content) > _ACTIVATION_IMAGE_MAX_BYTES:
        return error("图片大小必须不超过 5 MiB", status_code=400)
    # MIME alone is spoofable; require a matching image signature and never
    # accept SVG/XML/script or executable content.
    signatures = {
        "png": content.startswith(b"\x89PNG\r\n\x1a\n") and content[12:16] == b"IHDR" and len(content) >= 24,
        "jpg": content.startswith(b"\xff\xd8\xff") and content.endswith(b"\xff\xd9"),
        "webp": content.startswith(b"RIFF") and content[8:12] == b"WEBP" and len(content) >= 16,
        "gif": content.startswith((b"GIF87a", b"GIF89a")) and len(content) >= 10,
    }
    if not signatures.get(ext, False):
        return error("图片内容与 MIME 类型不匹配", status_code=400)
    root = app_settings().image_dir.resolve()
    target_dir = (root / "activation").resolve()
    if root not in target_dir.parents:
        return error("图片保存路径无效", status_code=500)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"scan-image.{ext}"
        old_meta = await _activation_image_settings(db)
        old_relative = old_meta.get("activation_image_path") or ""
        target.write_bytes(content)
    except OSError:
        return error("图片保存失败，请稍后重试", status_code=500)
    except Exception:
        logger.exception("管理端旧扫码图片元数据读取失败")
        return error("图片保存失败，请稍后重试", status_code=500)
    # Remove only a previous generated activation image in the fixed folder;
    # never touch arbitrary user files or secret.key.
    if old_relative and old_relative != target.relative_to(root).as_posix():
        old_path = (root / old_relative).resolve()
        if root in old_path.parents and old_path.parent == target_dir and old_path.name.startswith("scan-image."):
            try:
                old_path.unlink()
            except OSError:
                pass
    from datetime import datetime, timezone

    updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    values = {
        "activation_image_path": target.relative_to(root).as_posix(),
        "activation_image_updated_at": updated,
        "activation_image_mime": mime,
        "activation_image_size": str(len(content)),
    }
    for key, value in values.items():
        row = await db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=value))
        else:
            row.value = value
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.warning("扫码图片元数据保存失败 error_type=%s", type(exc).__name__)
        return error("图片保存失败，请稍后重试", status_code=500)
    return ok(
        {
            "scan_image_url": "/api/v1/settings/activation-image/file",
            "image_updated_at": updated,
            "mime": mime,
            "size": len(content),
            "target_width": 828,
            "target_height": 1124,
        }
    )


@router.delete("/settings/activation-image")
async def clear_activation_image(request: Request, db: DbSession) -> Any:
    """Clear the generated activation image without touching other image data."""
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    try:
        meta = await _activation_image_settings(db)
        relative = meta.get("activation_image_path") or ""
        root = app_settings().image_dir.resolve()
        if relative:
            old_path = (root / relative).resolve()
            if root in old_path.parents and old_path.parent == (root / "activation").resolve() and old_path.name.startswith("scan-image."):
                try:
                    old_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.warning("管理端扫码图片文件清理失败")
        for key in ("activation_image_path", "activation_image_updated_at", "activation_image_mime", "activation_image_size"):
            row = await db.get(AppSetting, key)
            if row is not None:
                row.value = ""
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("管理端扫码图片清空失败")
        return error("图片清空失败，请稍后重试", status_code=500)
    return ok(
        {
            "scan_image_url": "",
            "image_updated_at": None,
            "mime": None,
            "size": 0,
            "target_width": 828,
            "target_height": 1124,
        }
    )


def _runtime_value(data: dict[str, Any], name: str) -> str:
    value = str(data.get(name) or "").strip()
    return value or str(getattr(settings_store.current(), name, "") or "").strip()


def _submitted_bool(data: dict[str, Any], name: str, default: bool = False) -> bool:
    value = data.get(name)
    if value is None or value == "":
        return bool(getattr(settings_store.current(), name, default))
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@router.post("/settings/tmdb/test")
async def test_tmdb_connection(request: Request, db: DbSession) -> Any:
    """Test current or submitted TMDB settings without persisting them."""
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    try:
        started = time.monotonic()
        async with TmdbClient(
            api_key=_runtime_value(data, "tmdb_api_key"),
            proxy_url=_runtime_value(data, "tmdb_proxy_url"),
        ) as client:
            await client.test_connection()
        return ok({"message": "TMDB 连接成功", "elapsed_ms": round((time.monotonic() - started) * 1000)})
    except TmdbError as exc:
        return error(str(exc), status_code=400)
    except Exception:
        logger.exception("TMDB 连接测试失败")
        return error("TMDB 连接测试失败，请稍后重试", status_code=502)

@router.post("/settings/moviepilot/test")
async def test_moviepilot_connection(request: Request, db: DbSession) -> Any:
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    try:
        async with MoviePilotClient(url=_runtime_value(data, "moviepilot_url"), username=_runtime_value(data, "moviepilot_username"), password=_runtime_value(data, "moviepilot_password")) as client:
            await client.search_media("test", count=1)
        return ok({"message": "MoviePilot 连接成功"})
    except MoviePilotError as exc:
        return error(str(exc), status_code=400)


@router.post("/settings/wecom/test")
async def test_wecom_connection(request: Request, db: DbSession) -> Any:
    """Test WeCom credentials without saving or returning any credential."""
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    runtime = settings_store.current()
    try:
        config = WeComConfig(
            corp_id=_runtime_value(data, "wecom_corp_id"),
            agent_id=int(_runtime_value(data, "wecom_agent_id") or runtime.wecom_agent_id),
            secret=_runtime_value(data, "wecom_secret"),
            api_base_url=_runtime_value(data, "wecom_api_base_url"),
            proxy_url=_runtime_value(data, "notify_proxy_url") if _submitted_bool(data, "notify_wecom_use_proxy") else "",
        )
        await notify.test_wecom(config)
        return ok({"message": "企业微信连接成功"})
    except (ValueError, WeComError) as exc:
        return error(str(exc), status_code=400)
    except Exception:
        logger.exception("企业微信连接测试失败")
        return error("企业微信连接测试失败，请稍后重试", status_code=502)


def _wecom_menu_failure(action: str, exc: BaseException, *, status_code: int) -> Any:
    """Return the provider code and a short, redacted reason."""
    logger.warning(
        "企业微信应用菜单%s失败 error_type=%s",
        action,
        type(exc).__name__,
    )
    code = getattr(exc, "errcode", None)
    if code is None:
        match = re.search(r"错误码\s*([0-9]+)", str(exc))
        code = int(match.group(1)) if match else None
    reason = _redact_sensitive_text(str(exc))
    # Provider messages are not trusted. Remove configured credentials even
    # when a relay returns them without a ``key=value`` label.
    try:
        runtime = settings_store.current()
        for secret in (
            runtime.wecom_secret,
            runtime.wecom_token,
            runtime.wecom_encoding_aes_key,
            runtime.notify_proxy_url,
        ):
            if secret:
                reason = reason.replace(str(secret), "[REDACTED]")
    except Exception:
        pass
    reason = reason.replace("\n", " ").strip()[:240] or "未知原因"
    detail = f"（错误码 {code}：{reason}）" if code is not None else f"（{reason}）"
    return error(f"企业微信应用菜单{action}失败{detail}", status_code=status_code)


@router.post("/settings/wecom/menu/sync")
async def sync_wecom_menu_api(request: Request, db: DbSession) -> Any:
    """Create/replace the fixed application menu through WeCom OpenAPI."""
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    try:
        await notify.sync_wecom_menu()
        await services.log_action(db, "wecom_menu_synced", "同步企业微信应用菜单")
        return ok({"message": "企业微信应用菜单已同步"})
    except WeComError as exc:
        return _wecom_menu_failure("同步", exc, status_code=400)
    except Exception as exc:
        return _wecom_menu_failure("同步", exc, status_code=502)


@router.get("/settings/wecom/menu")
async def get_wecom_menu_api(request: Request, db: DbSession) -> Any:
    """Read the current application menu without exposing access credentials."""
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    try:
        menu = await notify.get_wecom_menu()
        return ok({"menu": menu})
    except WeComError as exc:
        return _wecom_menu_failure("读取", exc, status_code=400)
    except Exception as exc:
        return _wecom_menu_failure("读取", exc, status_code=502)


@router.post("/settings/wecom/menu/delete")
@router.delete("/settings/wecom/menu/delete")
async def delete_wecom_menu_api(request: Request, db: DbSession) -> Any:
    """Delete the current application menu through WeCom OpenAPI."""
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    try:
        await notify.delete_wecom_menu()
        await services.log_action(db, "wecom_menu_deleted", "删除企业微信应用菜单")
        return ok({"message": "企业微信应用菜单已删除"})
    except WeComError as exc:
        return _wecom_menu_failure("删除", exc, status_code=400)
    except Exception as exc:
        return _wecom_menu_failure("删除", exc, status_code=502)


@router.post("/settings/webhook/test")
@router.post("/settings/telegram/test")
@router.post("/settings/notifications/test")
async def test_notification_connection(request: Request, db: DbSession) -> Any:
    """Test one notification channel with the current or submitted settings."""
    if not _admin_required(request):
        return error("未登录或登录已失效", status_code=401)
    data, form_token = await payload(request)
    if not csrf_required(request, str(data.get("csrf_token") or form_token)):
        return error("CSRF 校验失败，请刷新页面重试", status_code=400)
    channel = str(data.get("channel") or "").strip().lower()
    # Dedicated paths provide an unambiguous default; the generic endpoint
    # requires an explicit channel to avoid accidentally sending to all.
    path = request.url.path.rsplit("/", 1)[-2]
    if path in {"webhook", "telegram"}:
        channel = path
    if channel not in {"webhook", "telegram"}:
        return error("通知类型无效", status_code=400)
    runtime = settings_store.current()
    try:
        if channel == "webhook":
            await notify.test_webhook(
                _runtime_value(data, "notify_webhook_url"),
                proxy=_runtime_value(data, "notify_proxy_url") if _submitted_bool(data, "notify_webhook_use_proxy") else "",
            )
        else:
            await notify.test_telegram(
                _runtime_value(data, "telegram_bot_token"),
                _runtime_value(data, "telegram_chat_id"),
                proxy=_runtime_value(data, "notify_proxy_url") if _submitted_bool(data, "notify_telegram_use_proxy") else "",
            )
        return ok({"channel": channel, "message": "通知连接成功"})
    except notify.NotificationTestError as exc:
        return error(str(exc), status_code=400)
    except Exception:
        logger.exception("通知连接测试失败 channel=%s", channel)
        return error("通知连接测试失败，请稍后重试", status_code=502)
