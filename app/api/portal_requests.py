"""Portal JSON API for TMDB search and media requests.

The Vue portal is the only client of these handlers.  Responses use a stable
``{ok, data, error}`` contract without exposing server credentials or another
user's private request notes.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from .. import services
from ..config import get_settings
from ..db import get_session
from ..models import ManagedUser, MediaRequest
from ..portal_routes import (
    _ensure_latest_runtime_settings,
    _media_request_json,
    _media_request_lists_json,
)
from ..security import csrf_ok, portal_user_id
from ..services import RegistrationError

router = APIRouter(tags=["portal-api"])
DbSession = Annotated[AsyncSession, Depends(get_session)]


def _ok(data: Any, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        {"ok": True, "data": data, "error": None}, status_code=status_code
    )


def _error(message: str, *, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "data": None, "error": message}, status_code=status_code
    )


async def _portal_api_user(request: Request, db: AsyncSession) -> ManagedUser | None:
    """Return the current portal user without redirecting API callers.

    API clients need a status response instead of a browser redirect, so this
    helper reports auth failure to each handler, which returns the common API
    envelope.
    """

    user_id = portal_user_id(request)
    if user_id is None:
        return None
    user = await db.get(ManagedUser, user_id)
    if user is None or not user.can_portal_login:
        request.session.clear()
        return None
    return user


async def _read_post_payload(request: Request) -> tuple[dict[str, Any], str]:
    """Read either JSON or a regular form request body."""

    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except ValueError:
            return {}, ""
        return (payload if isinstance(payload, dict) else {}), ""

    form = await request.form()
    return dict(form), str(form.get("csrf_token") or "")


@router.get("/requests")
async def requests_api(request: Request, db: DbSession) -> JSONResponse:
    user = await _portal_api_user(request, db)
    if user is None:
        return _error("未登录或登录已失效", status_code=401)

    lists = _media_request_lists_json(
        await services.portal_media_requests(db, user), viewer_user_id=user.id
    )
    return _ok(
        {
            "pending": lists["pending"],
            "in_library": lists["library"],
            "rejected": lists["rejected"],
        }
    )


@router.get("/requests/search")
async def request_search_api(
    request: Request,
    db: DbSession,
    mode: str = "multi",
    query: str = "",
    year: str = "",
) -> JSONResponse:
    user = await _portal_api_user(request, db)
    if user is None:
        return _error("未登录或登录已失效", status_code=401)

    await _ensure_latest_runtime_settings(db)
    try:
        results = await services.search_tmdb(mode, query, year)
    except RegistrationError as exc:
        return _error(str(exc), status_code=400)
    return _ok({"results": results})


@router.get("/requests/tmdb/{media_type}/{tmdb_id}")
async def request_tmdb_details_api(
    request: Request,
    media_type: str,
    tmdb_id: str,
    db: DbSession,
) -> JSONResponse:
    user = await _portal_api_user(request, db)
    if user is None:
        return _error("未登录或登录已失效", status_code=401)

    try:
        parsed_tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        return _error("TMDB ID 必须是正整数", status_code=400)
    if parsed_tmdb_id <= 0:
        return _error("TMDB ID 必须是正整数", status_code=400)

    await _ensure_latest_runtime_settings(db)
    try:
        detail = await services.tmdb_details(media_type, parsed_tmdb_id)
    except RegistrationError as exc:
        return _error(str(exc), status_code=400)
    return _ok(detail)


@router.post("/requests")
async def create_request_api(request: Request, db: DbSession) -> JSONResponse:
    user = await _portal_api_user(request, db)
    if user is None:
        return _error("未登录或登录已失效", status_code=401)

    payload, form_token = await _read_post_payload(request)
    csrf_token = str(
        payload.get("csrf_token")
        or form_token
        or request.headers.get("x-csrf-token")
        or ""
    )
    if not csrf_ok(request, csrf_token):
        return _error("CSRF 校验失败，请刷新页面重试", status_code=400)

    tmdb_id_raw = payload.get("tmdb_id")
    if isinstance(tmdb_id_raw, bool):
        return _error("作品信息无效", status_code=400)
    try:
        tmdb_id = int(str(tmdb_id_raw).strip())
    except (TypeError, ValueError):
        return _error("作品信息无效", status_code=400)

    media_type = str(payload.get("media_type") or "").strip()
    note = str(payload.get("note") or "")
    await _ensure_latest_runtime_settings(db)
    try:
        item = await services.create_media_request(
            db,
            user,
            tmdb_id=tmdb_id,
            media_type=media_type,
            note=note,
        )
    except RegistrationError as exc:
        return _error(str(exc), status_code=400)
    return _ok(_media_request_json(item), status_code=201)


@router.get("/requests/{request_id}/poster")
async def media_request_poster(request_id: int, request: Request, db: DbSession) -> Response:
    user = await _portal_api_user(request, db)
    if user is None:
        return _error("未登录或登录已失效", status_code=401)
    item = await db.get(MediaRequest, request_id)
    if item is None or item.server_id != user.server_id:
        return Response(status_code=404)
    if item.managed_user_id != user.id and item.status != "in_library":
        return Response(status_code=404)
    if not item.poster_local_path:
        return Response(status_code=404)
    root = get_settings().image_dir.resolve()
    path = (root / item.poster_local_path).resolve()
    if root not in path.parents or not path.is_file():
        return Response(status_code=404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
