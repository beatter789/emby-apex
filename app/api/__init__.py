"""Stable JSON API boundary used by the incremental frontend migration."""

from datetime import date as date_type
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..security import is_logged_in, issue_csrf_token, portal_user_id


DbSession = Annotated[AsyncSession, Depends(get_session)]
logger = logging.getLogger(__name__)


def _error(message: str, *, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "data": None, "error": message}, status_code=status_code
    )


def create_api_router(scope: str) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["api"])

    @router.get("/health")
    async def health() -> dict[str, object]:
        return {"ok": True, "data": {"service": "emby-apex", "scope": scope}, "error": None}

    @router.get("/auth/csrf")
    async def csrf(request: Request) -> JSONResponse:
        return JSONResponse({
            "ok": True,
            "data": {
                "csrf_token": issue_csrf_token(request),
                "authenticated": is_logged_in(request) or portal_user_id(request) is not None,
            },
            "error": None,
        })

    if scope == "admin":
        from .admin import router as admin_router

        router.include_router(admin_router)

        @router.get("/dashboard")
        async def dashboard_api(
            request: Request, db: DbSession, date: str | None = None
        ) -> JSONResponse:
            """Return the administrator dashboard without exposing credentials."""
            if not is_logged_in(request):
                return _error("未登录或登录已失效", status_code=401)

            # Import lazily to keep the API package independent from portal
            # authentication while reusing the dashboard snapshot helper.
            from .. import routes as admin_routes
            from ..stats import WatchDateError

            try:
                selected_date = date_type.fromisoformat(date) if date is not None else None
                if selected_date is not None and selected_date.isoformat() != date:
                    raise ValueError
            except ValueError:
                return _error("日期格式无效，请使用 YYYY-MM-DD", status_code=400)

            try:
                snapshot: dict[str, Any]
                if selected_date is None:
                    snapshot = await admin_routes._dashboard_snapshot(db)
                else:
                    snapshot = await admin_routes._dashboard_snapshot(db, selected_date)
                data = {
                    "summary": snapshot["summary"],
                    "sessions": snapshot["sessions"],
                    "watch_time": snapshot["watch_time"],
                    "trend": snapshot["trend"],
                    "servers": snapshot["servers"],
                    "logs": snapshot["logs"],
                    "poll_interval": snapshot["poll_interval"],
                }
                return JSONResponse({"ok": True, "data": data, "error": None})
            except WatchDateError as exc:
                return _error(str(exc), status_code=400)
            except Exception:
                logger.exception("管理端总览 API 生成失败")
                return _error("总览数据暂时不可用，请稍后重试", status_code=500)

    # Keep the portal business API behind the portal ASGI app only.  The
    # import is intentionally lazy so the admin app never imports portal
    # authentication or request handlers and the two route surfaces remain
    # isolated.
    if scope == "portal":
        from .portal import router as portal_router
        from .portal_requests import router as portal_requests_router

        router.include_router(portal_router)
        router.include_router(portal_requests_router)

    return router


__all__ = ["create_api_router"]
