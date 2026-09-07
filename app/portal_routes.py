"""Shared helpers for portal JSON APIs.

Portal pages are served by the static Vue shell in :mod:`app.shell`.  The
business endpoints live under ``/api/v1``; this module keeps the small set of
serializers and runtime-setting helpers shared by those endpoints without
registering legacy HTML routes.
"""

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from . import services, settings_store
from .models import ManagedUser, MediaRequest


async def _ensure_latest_runtime_settings(db: AsyncSession) -> None:
    """Load settings written by the separate admin process before TMDB calls."""
    runtime = settings_store.current()
    previous = (runtime.tmdb_api_key, runtime.tmdb_proxy_url)
    await settings_store.load(db)
    current = settings_store.current()
    if current.tmdb_api_key != previous[0] or current.tmdb_proxy_url != previous[1]:
        services.clear_tmdb_cache()


def _describe_status(user: ManagedUser) -> dict[str, str]:
    """Translate account flags into a user-facing status description."""
    expires_at = services.as_utc(user.expires_at)
    now = datetime.now(timezone.utc)
    if user.is_disabled:
        return {
            "tone": "bad",
            "label": "已停用",
            "hint": "账户被管理员停用，请联系管理员。",
        }
    if not user.is_activated:
        hint = "兑换任意兑换码即可开通播放权限。"
        if user.self_registered:
            hint += f"注册后 {services.activation_grace_hours()} 小时内未兑换将自动删除账户。"
        return {"tone": "pending", "label": "待激活", "hint": hint}
    if expires_at is not None and expires_at <= now:
        hint = "播放权限已关闭，兑换任意兑换码即可恢复。"
        if user.self_registered or user.is_claimed:
            hint = (
                f"播放权限已关闭，{services.delete_after_expiry_days()} "
                "天内未续期账户将被删除。"
            )
        return {"tone": "bad", "label": "已到期", "hint": hint}
    if not user.playback_enabled:
        return {
            "tone": "pending",
            "label": "播放已关闭",
            "hint": "账户可登录，但暂无播放权限。",
        }
    if expires_at is None:
        if user.is_claimed:
            return {
                "tone": "ok",
                "label": "正常",
                "hint": "已认领的账号，管理员未设到期时间，永久有效。",
            }
        return {"tone": "ok", "label": "正常", "hint": "当前无到期限制。"}
    remaining = expires_at - now
    return {
        "tone": "ok",
        "label": "正常",
        "hint": f"剩余 {remaining.days} 天 {remaining.seconds // 3600} 小时。",
    }


def _media_request_json(
    item: MediaRequest, *, viewer_user_id: int | None = None
) -> dict[str, object]:
    """Serialize a request while keeping another user's private fields hidden."""
    payload: dict[str, object] = {
        "id": item.id,
        "tmdb_id": item.tmdb_id,
        "media_type": item.media_type,
        "title": item.title,
        "original_title": item.original_title,
        "year": item.year,
        "overview": item.overview,
        "poster_url": item.poster_url,
        "poster_local_url": f"/api/v1/requests/{item.id}/poster" if item.poster_local_path else "",
        "status": item.status,
    }
    is_own = viewer_user_id is None or item.managed_user_id == viewer_user_id
    if is_own:
        payload.update(
            {
                "note": item.note,
                "created_at": item.created_at.isoformat(timespec="seconds") if item.created_at else None,
                "rejection_reason": item.rejection_reason,
            }
        )
    return payload


def _media_request_lists_json(
    lists: dict[str, list[MediaRequest]], *, viewer_user_id: int | None = None
) -> dict[str, list[dict[str, object]]]:
    return {
        name: [_media_request_json(item, viewer_user_id=viewer_user_id) for item in items]
        for name, items in lists.items()
    }


__all__ = [
    "_ensure_latest_runtime_settings",
    "_describe_status",
    "_media_request_json",
    "_media_request_lists_json",
]
