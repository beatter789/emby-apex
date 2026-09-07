"""Emby playback webhook endpoint.

The endpoint is intentionally registered on the administrator listener only.
Emby posts event JSON here; the portal listener never exposes this route.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import scheduler, services
from .db import get_session
from .logging_config import register_sensitive_values
from .models import Server

router = APIRouter(tags=["webhook"])
logger = logging.getLogger(__name__)

# This is the value used in the documented Emby webhook URL.  It is compared
# in constant time and is never written to logs or response bodies.
WEBHOOK_TOKEN = "embyapex"
DbSession = Annotated[AsyncSession, Depends(get_session)]
register_sensitive_values(WEBHOOK_TOKEN)


def _field(value: Any, *names: str) -> Any:
    if not isinstance(value, dict):
        return None
    lowered = {str(key).casefold(): item for key, item in value.items()}
    for name in names:
        if name in value:
            return value[name]
        found = lowered.get(name.casefold())
        if found is not None:
            return found
    return None


def _server_origin(value: Any) -> tuple[str, str, int | None] | None:
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        return parsed.scheme.lower(), parsed.hostname.casefold(), parsed.port
    except ValueError:
        return None


async def _resolve_server(db: AsyncSession, payload: dict[str, Any]) -> Server | None:
    """Resolve Emby's server metadata without requiring a schema migration."""
    servers = (
        await db.scalars(select(Server).where(Server.enabled.is_(True)).order_by(Server.id))
    ).all()
    if not servers:
        return None

    metadata = _field(payload, "Server", "server") or {}
    server_name = str(_field(metadata, "Name", "ServerName", "name") or "").strip().casefold()
    server_url = _field(metadata, "Url", "URL", "Address", "BaseUrl", "base_url") or _field(
        payload, "ServerUrl", "ServerURL", "server_url"
    )

    if not server_name:
        server_name = str(
            _field(payload, "ServerName", "server_name") or ""
        ).strip().casefold()

    if server_name:
        matches = [server for server in servers if server.name.casefold() == server_name]
        if len(matches) == 1:
            return matches[0]
    incoming_origin = _server_origin(server_url)
    if incoming_origin:
        scheme, host, port = incoming_origin
        matches = []
        for server in servers:
            origin = _server_origin(server.base_url)
            if origin and origin == (scheme, host, port):
                matches.append(server)
        if len(matches) == 1:
            return matches[0]
    # A single configured server is unambiguous even when an Emby webhook
    # plugin omits its Server object.
    return servers[0] if len(servers) == 1 else None


@router.post("/webhook")
async def emby_webhook(request: Request, db: DbSession) -> JSONResponse:
    token = str(request.query_params.get("token") or "")
    if not hmac.compare_digest(token.encode("utf-8"), WEBHOOK_TOKEN.encode("utf-8")):
        return JSONResponse(
            {"ok": False, "data": None, "error": "Webhook token 无效"},
            status_code=401,
        )

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            {"ok": False, "data": None, "error": "Webhook 请求不是有效 JSON"},
            status_code=400,
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            {"ok": False, "data": None, "error": "Webhook 请求格式无效"},
            status_code=400,
        )

    try:
        event_kind = services.webhook_event_kind(payload)
        if event_kind == "ignored":
            logger.info("Emby Webhook 事件已忽略 event=ignored")
            return JSONResponse(
                {"ok": True, "data": {"event": "ignored", "active": False, "ended": 0}, "error": None}
            )
        server = await _resolve_server(db, payload)
        if server is None:
            return JSONResponse(
                {"ok": False, "data": None, "error": "未找到匹配的 Emby 服务器"},
                status_code=404,
            )
        result = await services.process_webhook_event(db, server, payload)
        for key in result.get("ended_keys") or []:
            scheduler.remove_live_session(server.id, str(key[1]), str(key[2]))
        session = result.get("session")
        if session is not None:
            scheduler.update_live_session(session)
        # INFO 级别下可确认 Emby 测试/播放事件已经到达；只记录分类结果，
        # 不记录 Webhook 请求体、用户输入或 token 查询参数。
        logger.info(
            "Emby Webhook 事件已处理 server=%s event=%s active=%s ended=%s",
            server.name,
            result.get("kind") or "ignored",
            session is not None,
            len(result.get("ended_keys") or []),
        )
        return JSONResponse(
            {
                "ok": True,
                "data": {
                    "event": result.get("kind") or "ignored",
                    "active": session is not None,
                    "ended": len(result.get("ended_keys") or []),
                },
                "error": None,
            }
        )
    except Exception:
        # Never include the request body, token, or exception text in a
        # response.  The traceback remains available to server operators.
        logger.exception(
            "Emby webhook 处理失败 event=%s",
            services.webhook_event_kind(payload),
        )
        return JSONResponse(
            {"ok": False, "data": None, "error": "Webhook 处理失败，请稍后重试"},
            status_code=500,
        )


__all__ = ["router", "WEBHOOK_TOKEN"]
