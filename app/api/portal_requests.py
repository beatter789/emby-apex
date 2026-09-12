"""Portal JSON API for TMDB search and media requests.

The Vue portal is the only client of these handlers.  Responses use a stable
``{ok, data, error}`` contract without exposing server credentials or another
user's private request notes.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from .. import services
from ..config import get_settings
from ..db import get_session
from ..models import ManagedUser, MediaRequest, MediaRequestSummary
from ..portal_routes import (
    _ensure_latest_runtime_settings,
    _json_object,
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
    media_source: str = "themoviedb",
) -> JSONResponse:
    user = await _portal_api_user(request, db)
    if user is None:
        return _error("未登录或登录已失效", status_code=401)

    try:
        parsed_tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        if services.moviepilot_enabled():
            parsed_tmdb_id = 0
        else:
            return _error("TMDB ID 必须是正整数", status_code=400)

    await _ensure_latest_runtime_settings(db)
    try:
        detail = await services.tmdb_details(media_type, parsed_tmdb_id) if (parsed_tmdb_id and media_source in {"tmdb", "themoviedb"} and not services.moviepilot_enabled()) else await services.moviepilot_details(media_source, tmdb_id, media_type)
    except RegistrationError as exc:
        return _error(str(exc), status_code=400)
    return _ok(detail)


@router.get("/requests/{request_id}")
async def request_detail_api(
    request_id: int, request: Request, db: DbSession
) -> JSONResponse:
    """Read a persisted request snapshot, hydrating legacy rows once."""
    user = await _portal_api_user(request, db)
    if user is None:
        return _error("未登录或登录已失效", status_code=401)
    item = await db.get(MediaRequest, request_id) if request_id > 0 else None
    summary_row = None
    if item is None and request_id < 0:
        summary = await db.get(MediaRequestSummary, abs(request_id))
        summary_row = summary
        if summary is not None:
            # Terminal summaries are intentionally exposed through the same
            # snapshot serializer as live requests.  They carry no private
            # note or user ownership and remain visible only on their server.
            item = MediaRequest(
                id=-summary.id,
                server_id=summary.server_id,
                managed_user_id=user.id,
                tmdb_id=summary.tmdb_id,
                media_type=summary.media_type,
                title=summary.title,
                original_title=summary.original_title,
                year=summary.year,
                overview=summary.overview,
                poster_url=summary.poster_url,
                poster_local_path=summary.poster_local_path,
                note="",
                status=summary.status,
                confirmed_at=summary.processed_at if summary.status == "in_library" else None,
                confirmed_by=summary.processed_by if summary.status == "in_library" else None,
                rejection_reason=summary.rejection_reason,
                poster_error="",
                media_source="themoviedb",
                media_id=str(summary.tmdb_id),
                detail_snapshot=summary.detail_snapshot or "{}",
            )
    if item is None or item.server_id != user.server_id:
        return _error("求片记录不存在", status_code=404)
    if item.managed_user_id != user.id and item.status != "in_library":
        return _error("无权查看该求片记录", status_code=404)
    snapshot = _json_object(item.detail_snapshot)
    if not snapshot:
        # Rows created before snapshot support are repaired lazily.  The
        # marker prevents a permanently unavailable upstream from being
        # queried on every click.
        await _ensure_latest_runtime_settings(db)
        try:
            if services.moviepilot_enabled():
                detail = await services.moviepilot_details(
                    item.media_source,
                    item.media_id or str(item.tmdb_id),
                    item.media_type,
                )
            else:
                detail = await services.tmdb_details(item.media_type, item.tmdb_id)
        except RegistrationError:
            detail = {}
        if detail:
            snapshot = dict(detail)
            item.title = str(detail.get("title") or item.title or item.tmdb_id)
            item.original_title = str(detail.get("original_title") or item.original_title or "")
            item.year = detail.get("year") or item.year
            item.overview = str(detail.get("overview") or item.overview or "")
            item.poster_url = str(detail.get("poster_url") or item.poster_url or "")
            item.detail_snapshot = json.dumps(snapshot, ensure_ascii=False)
            if summary_row is not None:
                summary_row.detail_snapshot = item.detail_snapshot
                summary_row.title = item.title
                summary_row.original_title = item.original_title
                summary_row.year = item.year
                summary_row.overview = item.overview
                summary_row.poster_url = item.poster_url
        else:
            item.detail_snapshot = json.dumps({"_hydration_attempted": True}, ensure_ascii=False)
        await db.commit()
    return _ok(_media_request_json(item, viewer_user_id=user.id))


@router.get("/requests/{request_id}/season/{season_number}")
async def request_season_api(
    request_id: int, season_number: int, request: Request, db: DbSession
) -> JSONResponse:
    """Return cached episode metadata, hydrating a legacy snapshot once."""
    user = await _portal_api_user(request, db)
    if user is None:
        return _error("未登录或登录已失效", status_code=401)
    item = await db.get(MediaRequest, request_id) if request_id > 0 else None
    summary_row = None
    if item is None and request_id < 0:
        summary = await db.get(MediaRequestSummary, abs(request_id))
        summary_row = summary
        if summary is not None:
            item = MediaRequest(
                id=-summary.id,
                server_id=summary.server_id,
                managed_user_id=user.id,
                tmdb_id=summary.tmdb_id,
                media_type=summary.media_type,
                title=summary.title,
                original_title=summary.original_title,
                year=summary.year,
                overview=summary.overview,
                poster_url=summary.poster_url,
                poster_local_path=summary.poster_local_path,
                note="",
                status=summary.status,
                confirmed_at=summary.processed_at if summary.status == "in_library" else None,
                confirmed_by=summary.processed_by if summary.status == "in_library" else None,
                rejection_reason=summary.rejection_reason,
                poster_error="",
                media_source="themoviedb",
                media_id=str(summary.tmdb_id),
                detail_snapshot=summary.detail_snapshot or "{}",
            )
    if item is None or item.server_id != user.server_id:
        return _error("求片记录不存在", status_code=404)
    if item.managed_user_id != user.id and item.status != "in_library":
        return _error("无权查看该求片记录", status_code=404)
    if item.media_type != "tv" or season_number < 0:
        return _error("季集信息无效", status_code=400)
    snapshot = _json_object(item.detail_snapshot)
    episodes = snapshot.get("episodes_info")
    if isinstance(episodes, dict) and isinstance(episodes.get(str(season_number)), list):
        cached_rows = episodes[str(season_number)]
        # Older snapshots only contained episode numbers.  Treat those as
        # incomplete so the first expansion can hydrate the real names from
        # MoviePilot/TMDB and persist them for subsequent visits.
        if cached_rows and all(
            isinstance(row, dict) and str(row.get("name") or row.get("title") or "").strip()
            for row in cached_rows
        ):
            return _ok(cached_rows)
    if isinstance(episodes, list):
        rows = [row for row in episodes if isinstance(row, dict) and int(row.get("season_number", row.get("season", 1)) or 1) == season_number]
        if rows and all(
            isinstance(row, dict) and str(row.get("name") or row.get("title") or "").strip()
            for row in rows
        ):
            return _ok(rows)
    if not item.tmdb_id:
        snapshot.setdefault("episodes_info", {})
        if not isinstance(snapshot["episodes_info"], dict):
            snapshot["episodes_info"] = {}
        snapshot["episodes_info"][str(season_number)] = []
        item.detail_snapshot = json.dumps(snapshot, ensure_ascii=False)
        if summary_row is not None:
            summary_row.detail_snapshot = item.detail_snapshot
        await db.commit()
        return _ok([])
    await _ensure_latest_runtime_settings(db)
    try:
        if services.moviepilot_enabled():
            snapshot_group = str(snapshot.get("episode_group") or "").strip() or None
            async with services.MoviePilotClient() as client:
                rows = await client.season_episodes(
                    item.tmdb_id,
                    season_number,
                    episode_group=snapshot_group,
                )
        else:
            async with services.TmdbClient() as client:
                rows = await client.season_episodes(item.tmdb_id, season_number)
    except Exception:
        rows = []
    snapshot.setdefault("episodes_info", {})
    if not isinstance(snapshot["episodes_info"], dict):
        snapshot["episodes_info"] = {}
    snapshot["episodes_info"][str(season_number)] = rows
    item.detail_snapshot = json.dumps(snapshot, ensure_ascii=False)
    if summary_row is not None:
        summary_row.detail_snapshot = item.detail_snapshot
    await db.commit()
    return _ok(rows)


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

    tmdb_id_raw = payload.get("tmdb_id") or payload.get("media_id")
    if isinstance(tmdb_id_raw, bool):
        return _error("作品信息无效", status_code=400)
    try:
        tmdb_id = int(str(tmdb_id_raw).strip())
    except (TypeError, ValueError):
        if services.moviepilot_enabled() and payload.get("media_id"):
            tmdb_id = 0
        else:
            return _error("作品信息无效", status_code=400)

    media_type = str(payload.get("media_type") or "").strip()
    note = str(payload.get("note") or "")
    media_source = str(payload.get("media_source") or "themoviedb").strip()
    media_id = str(payload.get("media_id") or tmdb_id).strip()
    raw_seasons = payload.get("seasons") or []
    seasons = [int(x) for x in raw_seasons] if isinstance(raw_seasons, list) and all(str(x).lstrip("-").isdigit() for x in raw_seasons) else None
    detail_payload = payload.get("detail")
    if not isinstance(detail_payload, dict):
        detail_payload = None
    await _ensure_latest_runtime_settings(db)
    try:
        item = await services.create_media_request(
            db,
            user,
            tmdb_id=tmdb_id,
            media_type=media_type,
            note=note,
            media_source=media_source,
            media_id=media_id,
            seasons=seasons,
            detail_override=detail_payload,
        )
    except RegistrationError as exc:
        return _error(str(exc), status_code=400)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("MoviePilot 订阅提交失败")
        return _error("MoviePilot 订阅提交失败，请检查配置和权限", status_code=400)
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
