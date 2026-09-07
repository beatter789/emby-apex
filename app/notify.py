import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from . import settings_store
from .config import get_settings
from .logging_config import redact_text, register_sensitive_values
from .wecom import WeComClient, WeComConfig, WeComError

logger = logging.getLogger(__name__)
_startup_settings = get_settings()


NOTIFICATION_CATEGORIES = {
    "registration",
    "expiry",
    "media_request",
    "activation",
    "general",
}

# A bill code is committed before the notification is attempted.  Keep the
# idempotency marker in process memory so duplicate event delivery cannot send
# the same success message twice during the process lifetime.  Failed sends do
# not enter this set and can therefore be retried by the caller.
_activation_notification_lock = asyncio.Lock()
_activation_notified_bill_codes: set[str] = set()


async def _activation_notification_marked(bill_code: str) -> bool:
    """Read the durable sent marker without making the notification fatal."""
    try:
        from .db import SessionLocal
        from .models import BillCodeUsage
        from sqlalchemy import select

        async with SessionLocal() as db:
            usage = await db.scalar(
                select(BillCodeUsage).where(BillCodeUsage.bill_code == bill_code)
            )
            return bool(usage and usage.activation_notification_sent_at is not None)
    except Exception as exc:
        logger.warning(
            "账单码通知幂等标记读取失败 error_type=%s", type(exc).__name__
        )
        return False


async def _mark_activation_notification(bill_code: str) -> None:
    """Persist success after delivery; a marker failure never undoes delivery."""
    try:
        from .db import SessionLocal
        from .models import BillCodeUsage, utcnow
        from sqlalchemy import select

        async with SessionLocal() as db:
            usage = await db.scalar(
                select(BillCodeUsage).where(BillCodeUsage.bill_code == bill_code)
            )
            if usage is None or usage.activation_notification_sent_at is not None:
                return
            usage.activation_notification_sent_at = utcnow()
            await db.commit()
    except Exception as exc:
        logger.warning(
            "账单码通知幂等标记写入失败 error_type=%s", type(exc).__name__
        )


async def activation_success(event: dict[str, str]) -> bool:
    """Notification-agent contract for bill-code activation success.

    ``event`` contains ``username``, ``activated_at``, ``bill_code`` and
    ``redeem_code``.  The hook intentionally does not serialize codes into
    local logs; deployments may replace this function with a notification
    agent integration while the account transaction remains committed.
    """
    required = {"username", "activated_at", "bill_code", "redeem_code"}
    if not required.issubset(event):
        raise ValueError("activation event missing required fields")
    username = str(event.get("username") or "").strip()
    activated_at = str(event.get("activated_at") or "").strip()
    bill_code = str(event.get("bill_code") or "").strip()
    redeem_code = str(event.get("redeem_code") or "").strip()
    if not username or not activated_at or not bill_code or not redeem_code:
        raise ValueError("activation event contains empty fields")

    # The event contract carries an ISO timestamp.  Render only second
    # precision in the human-facing message while keeping the original value
    # out of logs.
    try:
        parsed_time = datetime.fromisoformat(activated_at.replace("Z", "+00:00"))
        if parsed_time.tzinfo is None:
            parsed_time = parsed_time.replace(tzinfo=timezone.utc)
        display_time = parsed_time.astimezone(ZoneInfo(_startup_settings.timezone)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except ValueError as exc:
        raise ValueError("activation event timestamp is invalid") from exc

    # The activation code is intentionally present in the notification, but
    # both codes must be redacted if a transport or logger echoes the message.
    register_sensitive_values(bill_code, redeem_code)
    message = (
        f"用户名：{username}\n"
        f"激活时间：{display_time}\n"
        f"激活码：{bill_code}\n"
        "已生成 30 天兑换码"
    )
    async with _activation_notification_lock:
        if bill_code in _activation_notified_bill_codes:
            return True
        if await _activation_notification_marked(bill_code):
            _activation_notified_bill_codes.add(bill_code)
            return True
        # A Telegram/Webhook success must not hide a transient WeCom failure
        # when multiple channels are enabled.
        sent = await push(
            "账单码激活成功",
            message,
            category="activation",
            required_channel="企业微信",
        )
        if sent:
            _activation_notified_bill_codes.add(bill_code)
            await _mark_activation_notification(bill_code)
        return sent


async def emit_activation_success(event: dict[str, str]) -> bool:
    """Event-oriented alias that remains patchable by notification agents."""
    return await activation_success(event)


class NotificationTestError(RuntimeError):
    """Safe, user-facing error raised by notification connection tests."""


async def push(
    title: str,
    message: str,
    *,
    category: str = "general",
    required_channel: str | None = None,
) -> bool:
    """尽力推送，失败只记日志，绝不让通知问题影响主流程。"""
    if category not in NOTIFICATION_CATEGORIES:
        category = "general"
    sent = False
    required_sent = False
    try:
        runtime = settings_store.current()
        register_sensitive_values(
            runtime.telegram_bot_token,
            runtime.wecom_secret,
            runtime.wecom_token,
            runtime.wecom_encoding_aes_key,
            runtime.notify_webhook_url,
            runtime.notify_proxy_url,
        )
    except Exception as exc:
        _log_failure(category, "配置", exc)
        return False

    channels = (
        (
            "Telegram",
            settings_store.notification_enabled(runtime, category, "telegram"),
            _send_telegram,
        ),
        (
            "Webhook",
            settings_store.notification_enabled(runtime, category, "webhook"),
            _send_webhook,
        ),
        (
            "企业微信",
            settings_store.notification_enabled(runtime, category, "wecom"),
            _send_wecom,
        ),
    )
    for channel, enabled, sender in channels:
        if not enabled:
            continue
        try:
            await sender(title, message)
            sent = True
            if channel == required_channel:
                required_sent = True
            logger.info(
                "通知发送成功 channel=%s title=%s message=%s",
                channel,
                redact_text(title),
                redact_text(message),
            )
        except Exception as exc:
            # 渠道实现本身已处理常见 HTTP 错误；这里兜底隔离配置/库版本等未知异常。
            # 不记录异常文本，避免 URL、Token 或代理凭据进入日志。
            _log_failure(category, channel, exc)
    return required_sent if required_channel else sent


def _log_failure(category: str, channel: str, exc: BaseException) -> None:
    logger.warning(
        "通知发送失败 category=%s channel=%s error_type=%s",
        category,
        channel,
        type(exc).__name__,
    )


async def _send_telegram(title: str, message: str) -> None:
    runtime = settings_store.current()
    register_sensitive_values(runtime.telegram_bot_token, runtime.notify_proxy_url)
    url = f"https://api.telegram.org/bot{runtime.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": runtime.telegram_chat_id,
        "text": f"*{title}*\n{message}",
        "parse_mode": "Markdown",
    }
    proxy = runtime.notify_proxy_url if runtime.notify_telegram_use_proxy else None
    async with httpx.AsyncClient(
        timeout=_startup_settings.http_timeout_seconds, proxy=proxy
    ) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()


async def _send_webhook(title: str, message: str) -> None:
    runtime = settings_store.current()
    register_sensitive_values(runtime.notify_webhook_url, runtime.notify_proxy_url)
    proxy = runtime.notify_proxy_url if runtime.notify_webhook_use_proxy else None
    async with httpx.AsyncClient(
        timeout=_startup_settings.http_timeout_seconds, proxy=proxy
    ) as client:
        response = await client.post(
            runtime.notify_webhook_url,
            json={"title": title, "message": message},
        )
        response.raise_for_status()


async def _send_wecom(title: str, message: str) -> None:
    runtime = settings_store.current()
    config = WeComConfig(
        corp_id=runtime.wecom_corp_id,
        agent_id=runtime.wecom_agent_id,
        secret=runtime.wecom_secret,
        api_base_url=runtime.wecom_api_base_url,
        proxy_url=runtime.notify_proxy_url if runtime.notify_wecom_use_proxy else "",
        token=runtime.wecom_token,
        encoding_aes_key=runtime.wecom_encoding_aes_key,
        admin_whitelist=runtime.wecom_admin_whitelist_ids,
    )
    async with WeComClient(config, timeout=_startup_settings.http_timeout_seconds) as client:
        await client.send_text(title, message, recipients=runtime.wecom_admin_whitelist_ids)
    logger.info(
        "企业微信通知消息发送成功 title=%s message=%s",
        redact_text(title),
        redact_text(message),
    )


async def send_wecom_to_user(userid: str, title: str, message: str) -> None:
    """向单个企业微信成员发送消息，用于回调命令的异步结果。"""
    target = userid.strip()
    if not target:
        raise WeComError("企业微信接收人 userid 不能为空")
    runtime = settings_store.current()
    if not (runtime.wecom_corp_id and runtime.wecom_agent_id and runtime.wecom_secret):
        raise WeComError("企业微信通知未配置完整")
    config = WeComConfig(
        corp_id=runtime.wecom_corp_id,
        agent_id=runtime.wecom_agent_id,
        secret=runtime.wecom_secret,
        api_base_url=runtime.wecom_api_base_url,
        proxy_url=runtime.notify_proxy_url if runtime.notify_wecom_use_proxy else "",
        token=runtime.wecom_token,
        encoding_aes_key=runtime.wecom_encoding_aes_key,
        admin_whitelist=runtime.wecom_admin_whitelist_ids,
    )
    async with WeComClient(config, timeout=_startup_settings.http_timeout_seconds) as client:
        await client.send_text(title, message, recipients=(target,))
    logger.info(
        "企业微信回复消息发送成功 userid=%s title=%s message=%s",
        target,
        redact_text(title),
        redact_text(message),
    )


async def test_wecom(config: WeComConfig) -> None:
    """只获取 access_token，用于设置页连接测试，不发送消息。"""
    async with WeComClient(config, timeout=_startup_settings.http_timeout_seconds) as client:
        await client.test_connection()
    logger.info("通知测试连接成功 channel=企业微信")


async def test_webhook(url: str, *, proxy: str = "") -> None:
    """Send a small test payload to a webhook without logging its URL."""
    target = url.strip()
    register_sensitive_values(target, proxy)
    if not target:
        raise NotificationTestError("Webhook 地址未配置")
    try:
        parsed = httpx.URL(target)
    except Exception as exc:
        raise NotificationTestError("Webhook 地址无效") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise NotificationTestError("Webhook 地址必须是 HTTP/HTTPS 地址")
    try:
        async with httpx.AsyncClient(
            timeout=_startup_settings.http_timeout_seconds, proxy=proxy.strip() or None
        ) as client:
            response = await client.post(
                target,
                json={"title": "Emby Apex 连接测试", "message": "Webhook 连接测试成功。"},
            )
            response.raise_for_status()
        logger.info("通知测试消息发送成功 channel=Webhook message=Webhook 连接测试成功。")
    except httpx.TimeoutException as exc:
        raise NotificationTestError("Webhook 请求超时，请检查网络或代理设置") from exc
    except httpx.HTTPStatusError as exc:
        raise NotificationTestError(f"Webhook 返回 HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise NotificationTestError("连接 Webhook 失败，请检查地址、网络或代理设置") from exc


async def test_telegram(bot_token: str, chat_id: str = "", *, proxy: str = "") -> None:
    """Validate a Telegram bot token without exposing it or sending a message."""
    token = bot_token.strip()
    register_sensitive_values(token, proxy)
    if not token:
        raise NotificationTestError("Telegram Bot Token 未配置")
    if not chat_id.strip():
        raise NotificationTestError("Telegram Chat ID 未配置")
    try:
        async with httpx.AsyncClient(
            timeout=_startup_settings.http_timeout_seconds, proxy=proxy.strip() or None
        ) as client:
            response = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            response.raise_for_status()
            body = response.json()
    except httpx.TimeoutException as exc:
        raise NotificationTestError("Telegram 请求超时，请检查网络或代理设置") from exc
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            raise NotificationTestError("Telegram Bot Token 无效") from exc
        raise NotificationTestError(f"Telegram 返回 HTTP {exc.response.status_code}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise NotificationTestError("连接 Telegram 失败，请检查网络或代理设置") from exc
    if not isinstance(body, dict) or not body.get("ok"):
        raise NotificationTestError("Telegram Bot Token 无效")
    logger.info("通知测试连接成功 channel=Telegram")


async def sync_wecom_menu() -> None:
    runtime = settings_store.current()
    if not (runtime.wecom_corp_id and runtime.wecom_agent_id and runtime.wecom_secret):
        raise WeComError("企业微信通知未配置完整")
    config = WeComConfig(
        corp_id=runtime.wecom_corp_id,
        agent_id=runtime.wecom_agent_id,
        secret=runtime.wecom_secret,
        api_base_url=runtime.wecom_api_base_url,
        proxy_url=runtime.notify_proxy_url if runtime.notify_wecom_use_proxy else "",
    )
    from .wecom import APPLICATION_MENU

    async with WeComClient(config, timeout=_startup_settings.http_timeout_seconds) as client:
        await client.create_menu(APPLICATION_MENU)


async def get_wecom_menu() -> dict:
    runtime = settings_store.current()
    config = WeComConfig(
        corp_id=runtime.wecom_corp_id,
        agent_id=runtime.wecom_agent_id,
        secret=runtime.wecom_secret,
        api_base_url=runtime.wecom_api_base_url,
        proxy_url=runtime.notify_proxy_url if runtime.notify_wecom_use_proxy else "",
    )
    async with WeComClient(config, timeout=_startup_settings.http_timeout_seconds) as client:
        return await client.get_menu()


async def delete_wecom_menu() -> None:
    runtime = settings_store.current()
    config = WeComConfig(
        corp_id=runtime.wecom_corp_id,
        agent_id=runtime.wecom_agent_id,
        secret=runtime.wecom_secret,
        api_base_url=runtime.wecom_api_base_url,
        proxy_url=runtime.notify_proxy_url if runtime.notify_wecom_use_proxy else "",
    )
    async with WeComClient(config, timeout=_startup_settings.http_timeout_seconds) as client:
        await client.delete_menu()
