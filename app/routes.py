"""Shared helpers for the admin API and the enterprise-WeChat callback.

The admin application is served by :mod:`app.shell` and the versioned JSON
API.  This module intentionally contains no page renderer or HTML route.  The
dashboard API imports its snapshot and sanitization helpers, while ``/wechat``
is registered by ``app.api.wechat``.
"""

from datetime import datetime, timezone
import asyncio
import logging
import re
from typing import Annotated
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import scheduler, services, settings_store, stats, wecom, wecom_commands
from .api.common import format_policy_detail, iso_seconds
from .db import get_session
from .models import ActionLog, ManagedUser, Server

logger = logging.getLogger(__name__)
DbSession = Annotated[AsyncSession, Depends(get_session)]

# The dashboard's "recent actions" is intentionally a short operational
# feed.  Detailed diagnostics remain available from the full logs endpoint.
MAJOR_ACTIONS = frozenset(
    {
        "server_added",
        "server_updated",
        "server_deleted",
        "server_enabled",
        "server_disabled",
        "codes_generated",
        "code_deleted",
        "redeem",
        "self_register",
        "admin_user_created",
        "claim_existing",
        "portal_login_enabled",
        "portal_login_disabled",
        "user_disabled",
        "user_enabled",
        "user_deleted",
        "playback_revoked",
        "user_policy_updated",
        "emby_policy_updated",
        "policy_updated",
        "media_request_confirmed",
        "media_request_rejected",
    }
)

_SENSITIVE_KEY_MARKERS = (
    "apikey",
    "accesstoken",
    "refreshtoken",
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "encodingaeskey",
    "corpsecret",
    "privatekey",
)
_SENSITIVE_KEY_PATTERN = (
    r"(?:api[_-]*key|access[_-]*token|refresh[_-]*token|token|secret|password|"
    r"passwd|authorization|encoding[_-]*aes[_-]*key|corp[_-]*secret|private[_-]*key)"
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    rf"(?P<prefix>[\"']?(?P<key>(?=[A-Za-z])[A-Za-z0-9_-]*?{_SENSITIVE_KEY_PATTERN}[A-Za-z0-9_-]*)[\"']?(?!\s*://)(?:\s*[:=]\s*|[ \t]+))"
    r"(?P<value>\"[^\"]*\"|'[^']*'|Bearer\s+[^,\s&}\"']+|[^,\s&}\"']+)",
    re.IGNORECASE,
)


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS)


def _redact_url_credentials(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return value
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        try:
            if parsed.port is not None:
                netloc += f":{parsed.port}"
        except ValueError:
            return value
        if parsed.username is not None or parsed.password is not None:
            netloc = "[REDACTED]@" + netloc
        query = urlencode(
            [
                (key, "[REDACTED]" if _is_sensitive_key(key) else item)
                for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            ],
            doseq=True,
        )
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))
    except (TypeError, ValueError):
        return value


def _redact_sensitive_text(value: str | None) -> str:
    """Redact credentials in action-log text before it crosses an API boundary."""
    if not value:
        return ""
    redacted = _URL_RE.sub(lambda match: _redact_url_credentials(match.group(0)), value)

    def replace_assignment(match: re.Match[str]) -> str:
        if not _is_sensitive_key(match.group("key")):
            return match.group(0)
        prefix = match.group("prefix")
        raw_value = match.group("value")
        if raw_value[:1] in {"\"", "'"} and raw_value[-1:] == raw_value[:1]:
            quote = raw_value[0]
            inner = raw_value[1:-1]
            bearer = re.match(r"(?P<scheme>Bearer)(?P<space>\s+)", inner, re.IGNORECASE)
            safe_value = (
                f"{bearer.group('scheme')}{bearer.group('space')}[REDACTED]"
                if bearer
                else "[REDACTED]"
            )
            return f"{prefix}{quote}{safe_value}{quote}"
        bearer = re.match(r"(?P<scheme>Bearer)(?P<space>\s+)", raw_value, re.IGNORECASE)
        safe_value = (
            f"{bearer.group('scheme')}{bearer.group('space')}[REDACTED]"
            if bearer
            else "[REDACTED]"
        )
        return f"{prefix}{safe_value}"

    return _SENSITIVE_ASSIGNMENT_RE.sub(replace_assignment, redacted)


def _parse_local_input(value: object | None) -> datetime | None:
    """Parse a local/ISO timestamp into UTC without dropping supplied offsets.

    ``datetime-local`` forms do not carry a timezone and are interpreted in
    the configured application timezone.  JSON callers may send an explicit
    offset (or ``null`` for a permanent account); in that case the supplied
    offset is authoritative.
    """
    from .config import get_settings
    from zoneinfo import ZoneInfo

    text = "" if value is None else str(value).strip()
    if not text:
        return None
    try:
        naive = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("到期时间格式不正确") from exc
    if naive.tzinfo is None:
        naive = naive.replace(tzinfo=ZoneInfo(get_settings().timezone))
    return naive.astimezone(timezone.utc)


def _safe_server_url(value: str) -> str:
    """Return only a server origin; never expose embedded credentials or query data."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    try:
        host = parsed.hostname.encode("idna").decode("ascii")
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        if parsed.port is not None:
            netloc += f":{parsed.port}"
    except (UnicodeError, ValueError):
        return ""
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


async def _dashboard_snapshot(db: AsyncSession) -> dict[str, object]:
    """Build the dashboard data returned by the versioned JSON API."""
    servers = (await db.scalars(select(Server).order_by(Server.name))).all()
    users = (await db.scalars(select(ManagedUser))).all()
    logs = (
        await db.scalars(
            select(ActionLog)
            .where(ActionLog.action.in_(MAJOR_ACTIONS))
            .order_by(ActionLog.created_at.desc())
            .limit(10)
        )
    ).all()
    now = services.utcnow()
    horizon = settings_store.current().expiry_remind_days
    expiring_soon = [
        user
        for user in users
        if not user.is_disabled
        and (expires := services.as_utc(user.expires_at)) is not None
        and 0 <= (expires - now).days <= horizon
    ]
    summary = {
        "servers": len(servers),
        "users": len(users),
        "disabled": sum(1 for user in users if user.is_disabled),
        "expiring": len(expiring_soon),
        "playing": len(scheduler.live_cache),
    }
    server_rows = [
        {
            "id": server.id,
            "name": server.name,
            "base_url": _safe_server_url(server.base_url),
            "enabled": bool(server.enabled),
            "status": "paused" if not server.enabled else "error" if server.last_error else "ok",
            "has_error": bool(server.last_error),
            "server_version": server.server_version,
            "last_ok_at": iso_seconds(server.last_ok_at),
        }
        for server in servers
    ]
    log_rows = [
        {
            "created_at": iso_seconds(log.created_at),
            "level": log.level,
            "action": log.action,
            "server_name": log.server_name,
            "username": log.username,
            "detail": format_policy_detail(log.detail),
        }
        for log in logs
    ]
    return {
        "summary": summary,
        "sessions": [session.__dict__.copy() for session in scheduler.live_cache],
        "watch_time": await stats.today_watch_time(db),
        "trend": await stats.daily_trend(db, 7),
        "servers": server_rows,
        "logs": log_rows,
        "poll_interval": settings_store.current().poll_interval_seconds,
    }


def _wecom_callback_settings() -> tuple[settings_store.RuntimeSettings, str, str]:
    runtime = settings_store.current()
    if not runtime.wecom_corp_id:
        raise wecom.WeComError("企业微信回调未配置企业 ID")
    if not runtime.wecom_token or not runtime.wecom_encoding_aes_key:
        raise wecom.WeComError("企业微信回调未配置 Token 和 EncodingAESKey")
    return runtime, runtime.wecom_token, runtime.wecom_encoding_aes_key


def _validate_callback_agent_id(value: str, expected: int) -> None:
    if not value:
        return
    try:
        agent_id = int(value)
    except (TypeError, ValueError) as exc:
        raise wecom.WeComError("企业微信回调 AgentID 格式无效") from exc
    if agent_id != expected:
        raise wecom.WeComError("企业微信回调 AgentID 不匹配")


def _encrypted_wecom_text_response(
    *,
    token: str,
    aes_key: str,
    content: str,
    from_user: str,
    corp_id: str,
) -> PlainTextResponse:
    response_xml = wecom.encrypted_text_reply(
        token,
        aes_key,
        content=wecom_commands.limit_reply(content),
        from_user=from_user,
        to_user=corp_id,
        receive_id=corp_id,
    )
    return PlainTextResponse(response_xml, media_type="application/xml")


async def _send_wecom_followups(userid: str, parts: tuple[str, ...]) -> None:
    """Deliver reply chunks after the passive callback response."""
    if not parts:
        return
    from . import notify

    for part in parts:
        try:
            await notify.send_wecom_to_user(userid, "企业微信操作提示", part)
        except Exception:
            # The passive first chunk has already been acknowledged.  Do not
            # expose provider credentials or exception text to the admin.
            logger.warning("企业微信分段回复发送失败 error_type=notification")
            return


def _schedule_wecom_followups(userid: str, content: str) -> str:
    parts = wecom_commands.split_reply(content)
    if len(parts) > 1:
        asyncio.create_task(_send_wecom_followups(userid, parts[1:]))
    return parts[0]


async def wecom_callback_verify(request: Request) -> PlainTextResponse:
    """Verify the enterprise-WeChat callback URL."""
    try:
        runtime, token, aes_key = _wecom_callback_settings()
        query = request.query_params
        signature = query.get("msg_signature", "")
        timestamp = query.get("timestamp", "")
        nonce = query.get("nonce", "")
        encrypted = query.get("echostr", "")
        if not all((signature, timestamp, nonce, encrypted)):
            raise wecom.WeComError("企业微信回调验证参数不完整")
        if not wecom.callback_timestamp_is_fresh(timestamp):
            raise wecom.WeComError("企业微信回调时间戳无效或已过期")
        if not wecom.verify_callback_signature(token, timestamp, nonce, encrypted, signature):
            raise wecom.WeComError("企业微信回调签名校验失败")
        plaintext, receive_id = wecom.decrypt_callback(aes_key, encrypted)
        if receive_id != runtime.wecom_corp_id:
            raise wecom.WeComError("企业微信回调 CorpID 不匹配")
        return PlainTextResponse(plaintext)
    except wecom.WeComError as exc:
        return PlainTextResponse(str(exc), status_code=403)
    except Exception:
        logger.warning("企业微信回调处理失败 error_type=unexpected")
        return PlainTextResponse("企业微信消息处理失败，请稍后重试", status_code=500)


async def wecom_callback_receive(request: Request, db: DbSession) -> PlainTextResponse:
    """Validate and process enterprise-WeChat text/menu commands."""
    try:
        runtime, token, aes_key = _wecom_callback_settings()
        query = request.query_params
        signature = query.get("msg_signature", "")
        timestamp = query.get("timestamp", "")
        nonce = query.get("nonce", "")
        encrypted, receive_id, outer_agent_id = wecom.parse_callback_xml(await request.body())
        if not all((signature, timestamp, nonce)):
            raise wecom.WeComError("企业微信回调验证参数不完整")
        if not wecom.callback_timestamp_is_fresh(timestamp):
            raise wecom.WeComError("企业微信回调时间戳无效或已过期")
        if not wecom.verify_callback_signature(token, timestamp, nonce, encrypted, signature):
            raise wecom.WeComError("企业微信回调签名校验失败")
        plaintext, decrypted_receive_id = wecom.decrypt_callback(aes_key, encrypted)
        if decrypted_receive_id != runtime.wecom_corp_id or (
            receive_id and receive_id != runtime.wecom_corp_id
        ):
            raise wecom.WeComError("企业微信回调 CorpID 不匹配")
        fields = wecom.parse_message_xml(plaintext)
        _validate_callback_agent_id(outer_agent_id, runtime.wecom_agent_id)
        message_agent_id = fields["AgentID"]
        if not message_agent_id:
            raise wecom.WeComError("企业微信回调消息缺少 AgentID")
        _validate_callback_agent_id(message_agent_id, runtime.wecom_agent_id)
        message_receive_id = fields["ToUserName"]
        if message_receive_id and message_receive_id != runtime.wecom_corp_id:
            raise wecom.WeComError("企业微信回调消息 CorpID 不匹配")
        from_user = fields["FromUserName"]
        msg_type = fields["MsgType"].lower()
        whitelist = set(runtime.wecom_admin_whitelist_ids)
        if not from_user:
            raise wecom.WeComError("企业微信回调缺少发送者")
        if whitelist and from_user not in whitelist:
            logger.warning("忽略非白名单企业微信回调：userid=%s", from_user)
            return PlainTextResponse("success")
        logger.info(
            "收到企业微信回调：userid=%s msg_type=%s agentid=%s",
            from_user,
            msg_type,
            message_agent_id,
        )
        if msg_type == "event" and fields["Event"].lower() not in {"click", "view"}:
            return PlainTextResponse("success")

        # A pending interactive flow belongs to this userid and takes
        # precedence over ordinary text commands until it is completed,
        # cancelled, or expires.  Menu clicks always replace the flow below.
        if msg_type == "text":
            interaction = await wecom_commands.consume_interaction(db, from_user, fields["Content"])
            if interaction is not None:
                if isinstance(interaction, wecom_commands.CommandRequest):
                    # Command args can contain a newly entered password.  Do
                    # not serialize args into logs or any callback response.
                    logger.info(
                        "企业微信交互完成 userid=%s command=%s",
                        from_user,
                        interaction.name,
                    )
                    asyncio.create_task(wecom_commands.run_background(interaction, from_user))
                    return PlainTextResponse("success")
                logger.info(
                    "企业微信回复消息 userid=%s message=%s",
                    from_user,
                    wecom_commands.limit_reply(interaction),
                )
                interaction = _schedule_wecom_followups(from_user, interaction)
                return _encrypted_wecom_text_response(
                    token=token,
                    aes_key=aes_key,
                    content=interaction,
                    from_user=from_user,
                    corp_id=runtime.wecom_corp_id,
                )

        command = wecom_commands.parse_command(
            fields["Content"] if msg_type == "text" else "",
            fields["EventKey"] if msg_type == "event" else "",
        )
        if command is None:
            return PlainTextResponse("success")
        interactive_flows = {
            "add_account",
            "delete_account",
            "enable_account",
            "disable_account",
            "code_generate",
            "code_delete",
            "request_pending",
        }
        if command.name in interactive_flows and not command.args:
            result = await wecom_commands.begin_interaction(db, from_user, command.name)
            result = _schedule_wecom_followups(from_user, result)
            logger.info(
                "企业微信回复消息 userid=%s message=%s",
                from_user,
                wecom_commands.limit_reply(result),
            )
            return _encrypted_wecom_text_response(
                token=token,
                aes_key=aes_key,
                content=result,
                from_user=from_user,
                corp_id=runtime.wecom_corp_id,
            )
        if wecom_commands.is_async_command(command):
            asyncio.create_task(wecom_commands.run_background(command, from_user))
            return PlainTextResponse("success")
        result = await wecom_commands.execute_quick(db, command)
        logger.info(
            "企业微信回复消息 userid=%s message=%s",
            from_user,
            wecom_commands.limit_reply(result),
        )
        return _encrypted_wecom_text_response(
            token=token,
            aes_key=aes_key,
            content=result,
            from_user=from_user,
            corp_id=runtime.wecom_corp_id,
        )
    except wecom.WeComError as exc:
        return PlainTextResponse(str(exc), status_code=403)
    except Exception:
        logger.warning("企业微信回调处理失败 error_type=unexpected")
        return PlainTextResponse("企业微信消息处理失败，请稍后重试", status_code=500)


__all__ = [
    "_dashboard_snapshot",
    "_parse_local_input",
    "_safe_server_url",
    "_redact_sensitive_text",
    "wecom_callback_verify",
    "wecom_callback_receive",
]
