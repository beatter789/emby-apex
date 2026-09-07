"""Shared helpers for the versioned JSON API boundary."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..security import csrf_ok, is_logged_in, issue_csrf_token, portal_user_id


def ok(data: Any, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True, "data": data, "error": None}, status_code=status_code)


def error(message: str, *, status_code: int) -> JSONResponse:
    return JSONResponse({"ok": False, "data": None, "error": message}, status_code=status_code)


async def payload(request: Request) -> tuple[dict[str, Any], str]:
    """Read JSON and form requests without maintaining two business paths."""
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            value = await request.json()
        except ValueError:
            return {}, ""
        return (value if isinstance(value, dict) else {}), ""
    try:
        form = await request.form()
    except (TypeError, ValueError):
        return {}, ""
    return dict(form), str(form.get("csrf_token") or "")


def csrf_token(request: Request, submitted: str = "") -> str:
    return submitted or request.headers.get("x-csrf-token", "") or str(request.query_params.get("csrf_token", ""))


def csrf_required(request: Request, submitted: str = "") -> bool:
    return csrf_ok(request, csrf_token(request, submitted))


def csrf_data(request: Request) -> dict[str, Any]:
    return {
        "csrf_token": issue_csrf_token(request),
        "authenticated": is_logged_in(request) or portal_user_id(request) is not None,
    }


def redact_log_detail(value: str | None) -> str:
    # Keep one implementation for API and enterprise-WeChat log consumers;
    # callers never serialize ActionLog.detail directly.
    from ..routes import _redact_sensitive_text

    return _redact_sensitive_text(value)


def iso_seconds(value: datetime | None) -> str | None:
    """Serialize API datetimes at second precision without changing storage."""
    if value is None:
        return None
    return value.isoformat(timespec="seconds")


_POLICY_LABELS = {
    "EnableMediaPlayback": "允许媒体播放",
    "EnableVideoPlaybackTranscoding": "视频转码",
    "EnableAudioPlaybackTranscoding": "音频转码",
    "EnablePlaybackRemuxing": "播放 Remux",
    "EnableMediaConversion": "媒体转换",
    "EnableSyncTranscoding": "同步转码",
    "ForceRemoteSourceTranscoding": "远程源强制转码",
    "EnableRemoteAccess": "远程访问",
    "EnableAllDevices": "全部设备",
    "EnabledDevices": "指定设备",
    "IsHidden": "本地登录隐藏",
    "IsHiddenRemotely": "远程登录隐藏",
    "IsHiddenFromUnusedDevices": "未使用设备隐藏",
    "EnableAllFolders": "全部媒体库",
    "EnabledFolders": "指定媒体库",
    "EnableDownloads": "下载",
    "EnableLiveTv": "直播电视",
    "EnablePublicSharing": "公开分享",
    "EnableRemoteControl": "远程控制",
    "EnableSharedDeviceControl": "共享设备控制",
    "EnableUserPreferenceAccess": "用户偏好",
    "EnableContentDeletion": "删除内容",
}


def _policy_value(value: Any) -> str:
    if isinstance(value, bool):
        return "开启" if value else "关闭"
    if value is None:
        return "空"
    if isinstance(value, (list, tuple, set)):
        return f"{len(value)} 项"
    if isinstance(value, dict):
        return f"{len(value)} 项"
    return str(value)


def format_policy_detail(value: str | None) -> str:
    """Compact JSON Policy details and translate common Emby field names."""
    detail = redact_log_detail(value)
    if not detail:
        return ""
    try:
        parsed = json.loads(detail)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        entries = []
        for key, item in parsed.items():
            label = _POLICY_LABELS.get(str(key))
            if label is None:
                # Unknown fields are retained by the backend but omitted from
                # the compact UI log unless they are harmless scalar values.
                if not isinstance(item, (str, int, float, bool)):
                    continue
                label = str(key)
            entries.append(f"{label}={_policy_value(item)}")
        return "Policy：" + "；".join(entries)

    # Existing action text often contains a comma-separated list of changed
    # Emby fields rather than a JSON object. Translate those names in place.
    for key, label in _POLICY_LABELS.items():
        detail = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])", label, detail)
    return re.sub(r"\s+", " ", detail).strip()


async def get_portal_user(request: Request, db: AsyncSession):
    from ..models import ManagedUser

    user_id = portal_user_id(request)
    if user_id is None:
        return None
    user = await db.get(ManagedUser, user_id)
    if user is None or not user.can_portal_login:
        request.session.clear()
        return None
    return user
