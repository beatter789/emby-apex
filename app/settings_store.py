"""运行时配置：管理员在后台改，改完立刻生效，不用重启容器。

与 `config.py` 的分工：
- `config.py` 是**启动期**配置，只管数据目录、端口、时区这类进程起来之前就要定下的值，
  仍然只能由环境变量给。
- 本模块是**运行期**配置，轮询间隔、宽限期、通知地址这类随时可调的值都在这里。
  环境变量只充当第一次启动的默认值，此后以数据库 `app_settings` 表为准。

内存里保留一份缓存，业务代码同步读取（`current()`），
避免在轮询热路径上每次都去查库。写入走 `save()`，落库后立即刷新缓存。
"""

import logging
from dataclasses import dataclass, fields
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .models import AppSetting

logger = logging.getLogger(__name__)
_env = get_settings()


@dataclass
class RuntimeSettings:
    """可在后台修改的配置项。字段名即 app_settings 表里的 key。"""

    poll_interval_seconds: int
    expiry_check_minutes: int
    expiry_remind_days: int
    activation_grace_hours: int
    purge_after_expiry_days: int
    history_retention_days: int
    registration_enabled: bool
    # 独立于 registration_enabled：停掉新放号后仍可让存量老用户自助认领。
    claim_enabled: bool
    # 关掉则 Emby 客户端里的「个人设置」不可用，用户只能在 portal 改密码。
    allow_emby_self_password: bool
    portal_password_min_length: int
    notify_webhook_url: str
    telegram_bot_token: str
    telegram_chat_id: str
    notify_proxy_url: str
    notify_webhook_use_proxy: bool
    notify_telegram_use_proxy: bool
    notify_wecom_use_proxy: bool
    notify_registration_webhook: bool
    notify_registration_telegram: bool
    notify_registration_wecom: bool
    notify_expiry_webhook: bool
    notify_expiry_telegram: bool
    notify_expiry_wecom: bool
    notify_media_request_webhook: bool
    notify_media_request_telegram: bool
    notify_media_request_wecom: bool
    notify_activation_webhook: bool
    notify_activation_telegram: bool
    notify_activation_wecom: bool
    notify_general_webhook: bool
    notify_general_telegram: bool
    notify_general_wecom: bool
    wecom_corp_id: str
    wecom_agent_id: int
    wecom_secret: str
    wecom_api_base_url: str
    # 旧版本字段：仅用于读取和迁移，不再在设置页显示。
    wecom_proxy_url: str
    wecom_token: str
    wecom_encoding_aes_key: str
    wecom_admin_whitelist: str
    tmdb_api_key: str
    tmdb_proxy_url: str
    tmdb_retention_days: int
    log_level: str

    @property
    def wecom_admin_whitelist_ids(self) -> tuple[str, ...]:
        from .wecom import parse_admin_whitelist

        return parse_admin_whitelist(self.wecom_admin_whitelist)


# 每项的取值范围与中文标签，供表单渲染与校验共用。
# (类型, 最小值, 最大值, 标签, 说明)
FIELD_SPECS: dict[str, tuple[str, int | None, int | None, str, str]] = {
    "poll_interval_seconds": ("int", 5, 600, "管理端总览刷新间隔（秒）", "仅用于管理端页面刷新当前播放，不产生播放记录写入"),
    "log_level": ("str", None, None, "日志级别", "可选 DEBUG、INFO、WARNING 或 ERROR；默认 WARNING"),
    "expiry_check_minutes": ("int", 1, 1440, "到期检查间隔（分钟）", "多久扫一遍到期用户"),
    "expiry_remind_days": ("int", 0, 60, "到期前提醒天数", "0 表示不提醒"),
    "activation_grace_hours": (
        "int",
        1,
        720,
        "注册未激活保留（小时）",
        "注册后这么久仍未兑换任何码即删号",
    ),
    "purge_after_expiry_days": (
        "int",
        0,
        365,
        "到期后保留天数",
        "到期先关播放，这么多天后仍未续期才删号，0 为到期即删",
    ),
    "history_retention_days": ("int", 1, 3650, "播放历史保留天数", "超期记录自动清理"),
    "registration_enabled": ("bool", None, None, "开放自助注册", "关掉后注册页返回 404"),
    "claim_enabled": (
        "bool",
        None,
        None,
        "开放老用户认领",
        "允许 Emby 已有账号凭原密码自助开通用户端登录，与自助注册各自独立",
    ),
    "allow_emby_self_password": (
        "bool",
        None,
        None,
        "允许在 Emby 客户端改密码",
        "默认关闭。Emby 接口无法读回密码明文，若允许用户在 Emby 侧自行修改，"
        "portal 密码会与 Emby 不一致。关闭后密码只能在用户端修改，由控制器同步给 Emby",
    ),
    "portal_password_min_length": ("int", 1, 128, "用户密码最短长度", "用户端注册与改密的下限"),
    "notify_webhook_url": ("str", None, None, "通知 Webhook 地址", "留空则不推送"),
    "telegram_bot_token": ("str", None, None, "Telegram Bot Token", "与 Chat ID 同时填才生效"),
    "telegram_chat_id": ("str", None, None, "Telegram Chat ID", ""),
    "notify_proxy_url": (
        "str", None, None, "通知 HTTP 代理地址", "Webhook、Telegram、企业微信可分别勾选是否使用",
    ),
    "notify_webhook_use_proxy": ("bool", None, None, "Webhook 使用通知代理", ""),
    "notify_telegram_use_proxy": ("bool", None, None, "Telegram 使用通知代理", ""),
    "notify_wecom_use_proxy": ("bool", None, None, "企业微信使用通知代理", ""),
    "notify_registration_webhook": ("bool", None, None, "注册通知 · Webhook", ""),
    "notify_registration_telegram": ("bool", None, None, "注册通知 · Telegram", ""),
    "notify_registration_wecom": ("bool", None, None, "注册通知 · 企业微信", ""),
    "notify_expiry_webhook": ("bool", None, None, "到期通知 · Webhook", ""),
    "notify_expiry_telegram": ("bool", None, None, "到期通知 · Telegram", ""),
    "notify_expiry_wecom": ("bool", None, None, "到期通知 · 企业微信", ""),
    "notify_media_request_webhook": ("bool", None, None, "求片通知 · Webhook", ""),
    "notify_media_request_telegram": ("bool", None, None, "求片通知 · Telegram", ""),
    "notify_media_request_wecom": ("bool", None, None, "求片通知 · 企业微信", ""),
    "notify_activation_webhook": ("bool", None, None, "账单码激活通知 · Webhook", ""),
    "notify_activation_telegram": ("bool", None, None, "账单码激活通知 · Telegram", ""),
    "notify_activation_wecom": ("bool", None, None, "账单码激活通知 · 企业微信", ""),
    "notify_general_webhook": ("bool", None, None, "其他通知 · Webhook", ""),
    "notify_general_telegram": ("bool", None, None, "其他通知 · Telegram", ""),
    "notify_general_wecom": ("bool", None, None, "其他通知 · 企业微信", ""),
    "wecom_corp_id": ("str", None, None, "企业微信企业 ID", "自建应用所属企业的 CorpID"),
    "wecom_agent_id": ("int", 1, 2_147_483_647, "企业微信应用 AgentID", "在应用详情页查看的整数 ID"),
    "wecom_secret": (
        "secret",
        None,
        None,
        "企业微信应用 Secret",
        "留空保持已配置值；企业 ID、AgentID、Secret 都填好才会发送通知",
    ),
    "wecom_api_base_url": (
        "str",
        None,
        None,
        "企业微信 API 中转地址",
        "填写 https://relay.example.com；留空使用 https://qyapi.weixin.qq.com",
    ),
    "wecom_proxy_url": ("str", None, None, "兼容旧企业微信代理地址", "旧版本字段，不在设置页显示"),
    "wecom_token": (
        "secret",
        None,
        None,
        "企业微信回调 Token",
        "仅用于回调验签；不接收回调时可留空",
    ),
    "wecom_encoding_aes_key": (
        "secret",
        None,
        None,
        "企业微信 EncodingAESKey",
        "仅用于回调解密；不接收回调时可留空",
    ),
    "wecom_admin_whitelist": (
        "str",
        None,
        None,
        "企业微信管理员白名单",
        "填写允许执行回调管理命令的 userid，英文逗号分隔；留空则允许所有成员",
    ),
    "tmdb_api_key": ("str", None, None, "TMDB API Key", "明文显示；留空并保存会清除 Key 并停用 TMDB"),
    "tmdb_proxy_url": (
        "str",
        None,
        None,
        "TMDB 代理地址",
        "可填 http://、https:// 或 socks5:// 代理，留空直连",
    ),
    "tmdb_retention_days": (
        "int",
        0,
        3650,
        "TMDB 求片历史保留天数",
        "清理已入库/已拒绝求片详情和本地海报；0 表示永久保留",
    ),
}


def _from_env() -> RuntimeSettings:
    """首次启动时用环境变量兜底，之后数据库里的值优先。"""
    return RuntimeSettings(
        poll_interval_seconds=_env.poll_interval_seconds,
        expiry_check_minutes=_env.expiry_check_minutes,
        expiry_remind_days=_env.expiry_remind_days,
        activation_grace_hours=_env.activation_grace_hours,
        purge_after_expiry_days=_env.purge_after_expiry_days,
        history_retention_days=_env.history_retention_days,
        registration_enabled=_env.registration_enabled,
        claim_enabled=False,
        allow_emby_self_password=False,
        portal_password_min_length=8,
        notify_webhook_url=_env.notify_webhook_url,
        telegram_bot_token=_env.telegram_bot_token,
        telegram_chat_id=_env.telegram_chat_id,
        notify_proxy_url="",
        notify_webhook_use_proxy=False,
        notify_telegram_use_proxy=False,
        notify_wecom_use_proxy=False,
        # Notification delivery is opt-in. A channel must also have its
        # credentials configured before notify.push() considers it enabled.
        notify_registration_webhook=False,
        notify_registration_telegram=False,
        notify_registration_wecom=False,
        notify_expiry_webhook=False,
        notify_expiry_telegram=False,
        notify_expiry_wecom=False,
        notify_media_request_webhook=False,
        notify_media_request_telegram=False,
        notify_media_request_wecom=False,
        notify_activation_webhook=False,
        notify_activation_telegram=False,
        notify_activation_wecom=False,
        notify_general_webhook=False,
        notify_general_telegram=False,
        notify_general_wecom=False,
        wecom_corp_id="",
        wecom_agent_id=1,
        wecom_secret="",
        wecom_api_base_url="https://qyapi.weixin.qq.com",
        wecom_proxy_url="",
        wecom_token="",
        wecom_encoding_aes_key="",
        wecom_admin_whitelist="",
        tmdb_api_key="",
        tmdb_proxy_url="",
        tmdb_retention_days=30,
        log_level="WARNING",
    )


_cache: RuntimeSettings = _from_env()


def current() -> RuntimeSettings:
    """业务代码统一从这里读，永远拿到最近一次刷新的值。"""
    return _cache


def notification_channel_configured(runtime: RuntimeSettings, channel: str) -> bool:
    """Return whether a channel has all credentials required for delivery."""
    if channel == "webhook":
        return bool(str(getattr(runtime, "notify_webhook_url", "") or "").strip())
    if channel == "telegram":
        return bool(
            str(getattr(runtime, "telegram_bot_token", "") or "").strip()
            and str(getattr(runtime, "telegram_chat_id", "") or "").strip()
        )
    if channel == "wecom":
        try:
            agent_id = int(getattr(runtime, "wecom_agent_id", 0) or 0)
        except (TypeError, ValueError):
            agent_id = 0
        return bool(
            str(getattr(runtime, "wecom_corp_id", "") or "").strip()
            and agent_id > 0
            and str(getattr(runtime, "wecom_secret", "") or "").strip()
        )
    return False


def notification_enabled(
    runtime: RuntimeSettings, category: str, channel: str
) -> bool:
    """Return the effective opt-in state exposed to routing and the UI."""
    if category not in {"registration", "expiry", "media_request", "activation", "general"}:
        return False
    return bool(
        getattr(runtime, f"notify_{category}_{channel}", False)
        and notification_channel_configured(runtime, channel)
    )


def _is_secret_field(name: str) -> bool:
    return FIELD_SPECS.get(name, ("", None, None, "", ""))[0] == "secret"


def _decrypt_stored_secret(name: str, raw: str) -> str:
    if not _is_secret_field(name) or not raw.startswith("enc:"):
        return raw
    from .security import decrypt_secret

    try:
        return decrypt_secret(raw[4:])
    except ValueError as exc:
        logger.warning("配置项 %s 无法解密，已按空值处理：%s", name, exc)
        return ""


def _coerce(name: str, raw: str) -> Any:
    kind, low, high, label, _ = FIELD_SPECS[name]
    raw_text = str(raw or "")
    if kind == "bool":
        return raw_text.strip().lower() in {"1", "true", "yes", "on"}
    if kind == "int":
        try:
            value = int(raw_text.strip())
        except ValueError as exc:
            raise ValueError(f"{label}必须是整数") from exc
        if low is not None and value < low:
            raise ValueError(f"{label}不能小于 {low}")
        if high is not None and value > high:
            raise ValueError(f"{label}不能大于 {high}")
        return value
    if name == "log_level":
        value = raw_text.strip().upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError(f"{label}必须是 DEBUG、INFO、WARNING 或 ERROR")
        return value
    if kind == "secret" and raw_text.strip():
        from .wecom import WeComError, validate_encoding_aes_key, validate_token

        try:
            if name == "wecom_token":
                return validate_token(raw_text)
            if name == "wecom_encoding_aes_key":
                return validate_encoding_aes_key(raw_text)
        except WeComError as exc:
            raise ValueError(str(exc)) from exc
    if name == "wecom_api_base_url" and raw_text.strip():
        from .wecom import WeComClient, WeComError

        try:
            WeComClient._validate_api_base_url(raw_text)
        except WeComError as exc:
            raise ValueError(str(exc)) from exc
    if name == "notify_proxy_url" and raw_text.strip():
        from .wecom import WeComClient, WeComError

        try:
            WeComClient._validate_proxy(raw_text)
        except WeComError as exc:
            raise ValueError(str(exc).replace("企业微信代理地址", "通知代理地址")) from exc
    return raw_text.strip()


def _serialize(name: str, value: Any) -> str:
    if isinstance(value, bool):
        serialized = "true" if value else "false"
    else:
        serialized = str(value)
    if _is_secret_field(name) and serialized:
        from .security import encrypt_secret

        return f"enc:{encrypt_secret(serialized)}"
    return serialized


async def load(db: AsyncSession) -> RuntimeSettings:
    """从库里读全量配置刷新缓存。启动时与每次保存后各调一次。"""
    global _cache
    rows = {row.key: row.value for row in (await db.scalars(select(AppSetting))).all()}
    merged = _from_env()
    for field in fields(RuntimeSettings):
        if field.name not in rows:
            continue
        try:
            raw = _decrypt_stored_secret(field.name, rows[field.name])
            setattr(merged, field.name, _coerce(field.name, raw))
        except ValueError as exc:
            # 库里存了坏值不该让整个应用起不来，退回环境变量默认值。
            logger.warning("配置项 %s 取值无效（%s），已回退默认值", field.name, exc)

    # 旧版本把“企业微信代理地址”同时用于 API 地址和网络代理。
    # 仅在新字段尚未配置时迁移，避免覆盖管理员已经明确设置的新值。
    legacy_proxy = str(rows.get("wecom_proxy_url") or "").strip()
    if not str(rows.get("wecom_api_base_url") or "").strip() and legacy_proxy:
        try:
            from .wecom import is_api_base_url

            if is_api_base_url(legacy_proxy):
                merged.wecom_api_base_url = legacy_proxy.rstrip("/")
            else:
                merged.notify_proxy_url = legacy_proxy
                if "notify_wecom_use_proxy" not in rows:
                    merged.notify_wecom_use_proxy = True
        except Exception:
            logger.warning("旧企业微信代理地址迁移失败，已保留原值")
    _cache = merged
    from .logging_config import set_log_level

    set_log_level(_cache.log_level)
    return _cache


async def save(db: AsyncSession, updates: dict[str, str]) -> RuntimeSettings:
    """写入并立即刷新缓存。任一项校验失败则整批不落库。"""
    parsed: dict[str, Any] = {}
    for name, raw in updates.items():
        if name not in FIELD_SPECS:
            continue
        parsed[name] = _coerce(name, raw)

    for name, value in parsed.items():
        row = await db.get(AppSetting, name)
        if row is None:
            db.add(AppSetting(key=name, value=_serialize(name, value)))
        else:
            row.value = _serialize(name, value)
    await db.commit()
    return await load(db)


def form_rows() -> list[dict[str, Any]]:
    """Return current setting values and field metadata for the admin API."""
    rows: list[dict[str, Any]] = []
    hidden = {"wecom_proxy_url"}
    for name, (kind, low, high, label, hint) in FIELD_SPECS.items():
        if name in hidden:
            continue
        value = getattr(_cache, name)
        if name.startswith("notify_"):
            for category in ("registration", "expiry", "media_request", "activation", "general"):
                prefix = f"notify_{category}_"
                if name.startswith(prefix):
                    channel = name[len(prefix):]
                    if channel in {"webhook", "telegram", "wecom"}:
                        value = notification_enabled(_cache, category, channel)
                    break
        rows.append(
            {
                "name": name,
                "kind": kind,
                "min": low,
                "max": high,
                "label": label,
                "hint": hint,
                # 不把 Secret 明文放进 API 字段，管理员输入框始终为空。
                "value": "" if kind == "secret" else value,
                "configured": bool(value) if kind == "secret" else True,
            }
        )
    return rows
