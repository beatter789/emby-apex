"""企业微信应用消息客户端及回调加解密工具。"""

from __future__ import annotations

import base64
import binascii
import asyncio
import hashlib
import hmac
import logging
import secrets
import struct
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .logging_config import register_sensitive_values

logger = logging.getLogger(__name__)

API_ROOT = "https://qyapi.weixin.qq.com"
TOKEN_ERROR_CODES = {40014, 42001, 42007, 42009}
MENU_TYPES = {
    "click",
    "view",
    "scancode_push",
    "scancode_waitmsg",
    "pic_sysphoto",
    "pic_photo_or_album",
    "pic_weixin",
    "location_select",
    "media_id",
    "view_limited",
    "miniprogram",
}

APPLICATION_MENU: dict[str, Any] = {
    "button": [
        {
            "name": "用户管理",
            "sub_button": [
                {"type": "click", "name": "增加用户", "key": "user_add"},
                {"type": "click", "name": "删除用户", "key": "user_delete"},
                {"type": "click", "name": "启用账户", "key": "user_enable"},
                {"type": "click", "name": "停用账户", "key": "user_disable"},
            ],
        },
        {
            "name": "账号激活",
            "sub_button": [
                {"type": "click", "name": "生成激活码", "key": "code_generate"},
                {"type": "click", "name": "删除激活码", "key": "code_delete"},
                {"type": "click", "name": "查询激活码", "key": "code_query"},
            ],
        },
        {
            "name": "求片",
            "sub_button": [
                {"type": "click", "name": "正在求片", "key": "request_pending"},
                {"type": "click", "name": "已求片", "key": "request_in_library"},
            ],
        },
    ]
}


def _menu_text(value: Any, *, field: str, max_bytes: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WeComError(f"企业微信菜单字段 {field} 必须是非空字符串")
    text = value.strip()
    if len(text.encode("utf-8")) > max_bytes:
        raise WeComError(f"企业微信菜单字段 {field} 超过 {max_bytes} 字节限制")
    return text


def _validate_menu_button(button: Any, *, top_level: bool) -> None:
    if not isinstance(button, dict):
        raise WeComError("企业微信菜单按钮必须是对象")
    _menu_text(button.get("name"), field="name", max_bytes=16 if top_level else 40)
    sub_button = button.get("sub_button")
    if top_level and sub_button is not None:
        if set(button) - {"name", "sub_button"}:
            raise WeComError("带 sub_button 的一级菜单不能包含 type、key 或其他按钮字段")
        if not isinstance(sub_button, list) or not 1 <= len(sub_button) <= 5:
            raise WeComError("企业微信二级菜单 sub_button 数量必须为 1-5 个")
        for child in sub_button:
            _validate_menu_button(child, top_level=False)
        return
    if "sub_button" in button:
        raise WeComError("二级菜单不能继续嵌套 sub_button")
    button_type = button.get("type")
    if not isinstance(button_type, str) or button_type not in MENU_TYPES:
        raise WeComError("企业微信菜单 type 无效")
    required_fields = {"type", "name"}
    if button_type in {
        "click",
        "scancode_push",
        "scancode_waitmsg",
        "pic_sysphoto",
        "pic_photo_or_album",
        "pic_weixin",
        "location_select",
    }:
        required_fields.add("key")
        _menu_text(button.get("key"), field="key", max_bytes=128)
    elif button_type in {"media_id", "view_limited"}:
        required_fields.add("media_id")
        _menu_text(button.get("media_id"), field="media_id", max_bytes=128)
    elif button_type == "view":
        required_fields.add("url")
        _menu_text(button.get("url"), field="url", max_bytes=1024)
    elif button_type == "miniprogram":
        required_fields.update({"url", "appid", "pagepath"})
        _menu_text(button.get("url"), field="url", max_bytes=1024)
        _menu_text(button.get("appid"), field="appid", max_bytes=128)
        _menu_text(button.get("pagepath"), field="pagepath", max_bytes=128)
    if set(button) != required_fields:
        raise WeComError("企业微信菜单按钮字段与 type 不匹配")


def validate_menu(menu: Any) -> None:
    """Validate the application-menu shape required by WeCom's create API."""
    if not isinstance(menu, dict) or set(menu) != {"button"}:
        raise WeComError("企业微信菜单必须只包含 button 字段")
    buttons = menu.get("button")
    if not isinstance(buttons, list) or not 1 <= len(buttons) <= 3:
        raise WeComError("企业微信一级菜单 button 数量必须为 1-3 个")
    for button in buttons:
        _validate_menu_button(button, top_level=True)

def is_api_base_url(value: str) -> bool:
    """判断旧配置是否是企业微信官方 API 地址，而不是网络代理。"""
    try:
        parsed = httpx.URL(value.strip())
    except Exception:
        return False
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.host
        and parsed.host.lower() == "qyapi.weixin.qq.com"
    )


class WeComError(RuntimeError):
    """企业微信配置或 API 调用错误。"""

    def __init__(self, message: str, *, errcode: int | None = None) -> None:
        super().__init__(message)
        self.errcode = errcode


validate_menu(APPLICATION_MENU)


def validate_token(value: str) -> str:
    token = value.strip()
    if not token:
        raise WeComError("企业微信回调 Token 不能为空")
    if len(token) > 32 or not token.isascii() or not token.isalnum():
        raise WeComError("企业微信回调 Token 必须是 1-32 位字母或数字")
    return token


def _aes_key(value: str) -> bytes:
    key_text = value.strip()
    if len(key_text) != 43:
        raise WeComError("企业微信 EncodingAESKey 必须是 43 位")
    try:
        key = base64.b64decode(key_text + "=", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise WeComError("企业微信 EncodingAESKey 格式无效") from exc
    if len(key) != 32:
        raise WeComError("企业微信 EncodingAESKey 解码后长度无效")
    return key


def validate_encoding_aes_key(value: str) -> str:
    _aes_key(value)
    return value.strip()


def parse_admin_whitelist(value: str) -> tuple[str, ...]:
    """把设置页的逗号分隔 userid 规范化，顺序保持稳定并去重。"""
    result: list[str] = []
    seen: set[str] = set()
    for item in value.split(","):
        user_id = item.strip()
        if user_id and user_id not in seen:
            seen.add(user_id)
            result.append(user_id)
    return tuple(result)


def callback_signature(token: str, timestamp: str, nonce: str, encrypted: str) -> str:
    values = sorted((token, timestamp, nonce, encrypted))
    return hashlib.sha1("".join(values).encode("utf-8")).hexdigest()


def verify_callback_signature(
    token: str,
    timestamp: str,
    nonce: str,
    encrypted: str,
    signature: str,
) -> bool:
    expected = callback_signature(token, timestamp, nonce, encrypted)
    return hmac.compare_digest(expected, signature.strip().lower())


def callback_timestamp_is_fresh(timestamp: str, *, max_age_seconds: int = 600) -> bool:
    """校验回调时间戳，降低截获请求被重放的风险。"""
    try:
        value = int(timestamp)
    except (TypeError, ValueError):
        return False
    return abs(time.time() - value) <= max_age_seconds


def decrypt_callback(encoding_aes_key: str, encrypted: str) -> tuple[str, str]:
    """解密企业微信回调，返回 (消息明文, receiveid)。"""
    key = _aes_key(encoding_aes_key)
    try:
        cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
        decryptor = cipher.decryptor()
        padded = decryptor.update(base64.b64decode(encrypted, validate=True)) + decryptor.finalize()
        unpadder = padding.PKCS7(256).unpadder()
        plain = unpadder.update(padded) + unpadder.finalize()
    except (ValueError, binascii.Error) as exc:
        raise WeComError("企业微信回调密文解密失败") from exc

    if len(plain) < 20:
        raise WeComError("企业微信回调明文长度无效")
    message_length = struct.unpack("!I", plain[16:20])[0]
    end = 20 + message_length
    if end > len(plain):
        raise WeComError("企业微信回调消息长度无效")
    try:
        message = plain[20:end].decode("utf-8")
        receive_id = plain[end:].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WeComError("企业微信回调明文编码无效") from exc
    return message, receive_id


def encrypt_callback(encoding_aes_key: str, message: str, receive_id: str) -> str:
    """按企业微信格式加密被动回复消息。"""
    key = _aes_key(encoding_aes_key)
    message_bytes = message.encode("utf-8")
    plain = secrets.token_bytes(16) + struct.pack("!I", len(message_bytes))
    plain += message_bytes + receive_id.encode("utf-8")
    padder = padding.PKCS7(256).padder()
    padded = padder.update(plain) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def encrypted_text_reply(
    token: str,
    encoding_aes_key: str,
    *,
    content: str,
    from_user: str,
    to_user: str,
    receive_id: str,
    timestamp: str | None = None,
    nonce: str | None = None,
) -> str:
    """构造企业微信被动文本回复 XML。"""
    timestamp = timestamp or str(int(time.time()))
    nonce = nonce or secrets.token_urlsafe(12)
    plaintext = (
        "<xml>"
        f"<ToUserName><![CDATA[{_cdata(from_user)}]]></ToUserName>"
        f"<FromUserName><![CDATA[{_cdata(to_user)}]]></FromUserName>"
        f"<CreateTime>{timestamp}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{_cdata(content)}]]></Content>"
        "</xml>"
    )
    encrypted = encrypt_callback(encoding_aes_key, plaintext, receive_id)
    signature = callback_signature(token, timestamp, nonce, encrypted)
    return (
        "<xml>"
        f"<Encrypt><![CDATA[{encrypted}]]></Encrypt>"
        f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
        f"<TimeStamp>{timestamp}</TimeStamp>"
        f"<Nonce><![CDATA[{_cdata(nonce)}]]></Nonce>"
        "</xml>"
    )


def _cdata(value: str) -> str:
    # XML CDATA cannot contain the closing marker. Split it into two adjacent
    # CDATA sections while keeping the resulting text unchanged.
    return str(value).replace("]]>", "]]]]><![CDATA[>")


def parse_message_xml(plaintext: str) -> dict[str, str]:
    """提取消息/菜单事件所需字段，不保留正文以外的扩展结构。"""
    try:
        root = ET.fromstring(plaintext)
    except ET.ParseError as exc:
        raise WeComError("企业微信消息明文 XML 格式无效") from exc
    if root.tag.rsplit("}", 1)[-1] != "xml":
        raise WeComError("企业微信消息明文 XML 根节点无效")
    names = (
        "FromUserName",
        "ToUserName",
        "MsgType",
        "AgentID",
        "Content",
        "Event",
        "EventKey",
    )
    return {name: (root.findtext(name) or "").strip() for name in names}


def parse_callback_xml(body: bytes) -> tuple[str, str, str]:
    """解析回调 XML，返回 (Encrypt, ToUserName, AgentID)。"""
    stripped = body.lstrip()
    if not stripped:
        raise WeComError("企业微信回调请求体为空")
    if stripped.startswith((b"{", b"[")):
        raise WeComError("企业微信回调仅支持 XML，不支持 JSON")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise WeComError("企业微信回调 XML 格式无效") from exc
    if root.tag.rsplit("}", 1)[-1] != "xml":
        raise WeComError("企业微信回调 XML 根节点无效")
    encrypted = (root.findtext("Encrypt") or "").strip()
    receive_id = (root.findtext("ToUserName") or "").strip()
    agent_id = (root.findtext("AgentID") or "").strip()
    if not encrypted:
        raise WeComError("企业微信回调缺少 Encrypt")
    return encrypted, receive_id, agent_id


@dataclass(frozen=True)
class WeComConfig:
    corp_id: str
    agent_id: int
    secret: str
    proxy_url: str = ""
    token: str = ""
    encoding_aes_key: str = ""
    admin_whitelist: tuple[str, ...] = ()
    api_base_url: str = API_ROOT

    @property
    def callback_enabled(self) -> bool:
        return bool(self.token and self.encoding_aes_key)


@dataclass
class _TokenEntry:
    value: str
    expires_at: float


class WeComClient:
    """企业微信 API 客户端，进程内缓存 access_token 并在失效时重试一次。"""

    _token_cache: dict[tuple[str, int, str, str], _TokenEntry] = {}
    _token_locks: dict[tuple[str, int, str, str], asyncio.Lock] = {}

    def __init__(self, config: WeComConfig, *, timeout: float = 12.0) -> None:
        corp_id = config.corp_id.strip()
        secret = config.secret.strip()
        if not corp_id or not secret:
            raise WeComError("企业微信企业ID和应用 Secret 不能为空")
        if config.agent_id <= 0:
            raise WeComError("企业微信应用 AgentID 必须是正整数")
        self.config = WeComConfig(
            corp_id=corp_id,
            agent_id=config.agent_id,
            secret=secret,
            api_base_url=self._validate_api_base_url(config.api_base_url),
            proxy_url=self._validate_proxy(config.proxy_url),
            token=config.token,
            encoding_aes_key=config.encoding_aes_key,
            admin_whitelist=config.admin_whitelist,
        )
        register_sensitive_values(
            corp_id,
            secret,
            config.proxy_url,
            config.token,
            config.encoding_aes_key,
        )
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @staticmethod
    def _validate_api_base_url(value: str) -> str:
        base = value.strip() or API_ROOT
        try:
            parsed = httpx.URL(base)
        except Exception as exc:
            raise WeComError("企业微信 API 中转地址无效") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise WeComError("企业微信 API 中转地址必须是 HTTP/HTTPS 地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise WeComError("企业微信 API 中转地址不能包含账号、查询参数或片段")
        return base.rstrip("/")

    @staticmethod
    def _validate_proxy(value: str) -> str:
        proxy = value.strip()
        if not proxy:
            return ""
        try:
            parsed = httpx.URL(proxy)
        except Exception as exc:
            raise WeComError("企业微信代理地址无效") from exc
        if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.host:
            raise WeComError("企业微信代理地址无效")
        return proxy

    async def __aenter__(self) -> "WeComClient":
        self._client = httpx.AsyncClient(
            base_url=self.config.api_base_url,
            timeout=self.timeout,
            headers={"Accept": "application/json"},
            proxy=self.config.proxy_url or None,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if self._client is None:
            raise WeComError("WeComClient 必须在 async with 上下文中使用")
        # Keep an optional path prefix in API relay URLs.  Passing a leading
        # slash directly to httpx would discard that prefix.
        request_url = self.config.api_base_url.rstrip("/") + "/" + path.lstrip("/")
        try:
            response = await self._client.request(method, request_url, **kwargs)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            logger.warning("企业微信请求超时（%s）", path)
            raise WeComError("企业微信请求超时，请检查网络或代理设置") from exc
        except httpx.HTTPStatusError as exc:
            logger.warning("企业微信返回 HTTP %s（%s）", exc.response.status_code, path)
            raise WeComError(f"企业微信请求失败（HTTP {exc.response.status_code}）") from exc
        except httpx.HTTPError as exc:
            logger.warning("企业微信连接失败（%s，%s）", path, type(exc).__name__)
            raise WeComError("连接企业微信失败，请检查网络或代理设置") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise WeComError("企业微信返回格式不正确") from exc
        if not isinstance(data, dict):
            raise WeComError("企业微信返回格式不正确")
        return data

    @property
    def _cache_key(self) -> tuple[str, int, str, str]:
        return (
            self.config.corp_id,
            self.config.agent_id,
            self.config.secret,
            self.config.api_base_url,
        )

    @staticmethod
    def _errcode(data: dict[str, Any]) -> int:
        try:
            return int(data.get("errcode", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise WeComError("企业微信返回的错误码格式不正确") from exc

    async def access_token(self, *, force_refresh: bool = False) -> str:
        now = time.monotonic()
        if not force_refresh:
            entry = self._token_cache.get(self._cache_key)
            if entry and entry.expires_at > now:
                return entry.value

        lock = self._token_locks.setdefault(self._cache_key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            entry = self._token_cache.get(self._cache_key)
            if not force_refresh and entry and entry.expires_at > now:
                return entry.value
            data = await self._request_json(
                "GET",
                "/cgi-bin/gettoken",
                params={"corpid": self.config.corp_id, "corpsecret": self.config.secret},
            )
            errcode = self._errcode(data)
            if errcode:
                raise WeComError(f"企业微信获取 access_token 失败（错误码 {errcode}）")
            token = str(data.get("access_token") or "")
            if not token:
                raise WeComError("企业微信未返回 access_token")
            register_sensitive_values(token)
            try:
                expires_in = max(60, int(data.get("expires_in", 7200) or 7200))
            except (TypeError, ValueError) as exc:
                raise WeComError("企业微信返回的 expires_in 格式不正确") from exc
            self._token_cache[self._cache_key] = _TokenEntry(
                value=token,
                expires_at=time.monotonic() + max(1, expires_in - 60),
            )
            return token

    async def test_connection(self) -> None:
        # 遵循企业微信的调用限制，测试也复用缓存，不因重复点击频繁调用 gettoken。
        await self.access_token()

    async def create_menu(self, menu: dict[str, Any]) -> None:
        """创建或覆盖当前应用菜单。"""
        validate_menu(menu)
        token = await self.access_token()
        data = await self._request_json(
            "POST",
            "/cgi-bin/menu/create",
            params={"access_token": token, "agentid": self.config.agent_id},
            json=menu,
        )
        errcode = self._errcode(data)
        if errcode in TOKEN_ERROR_CODES:
            token = await self.access_token(force_refresh=True)
            data = await self._request_json(
                "POST", "/cgi-bin/menu/create",
                params={"access_token": token, "agentid": self.config.agent_id},
                json=menu,
            )
            errcode = self._errcode(data)
        if errcode:
            raise WeComError(
                f"企业微信创建应用菜单失败（错误码 {errcode}：{data.get('errmsg') or 'unknown'}）",
                errcode=errcode,
            )

    async def get_menu(self) -> dict[str, Any]:
        token = await self.access_token()
        data = await self._request_json(
            "GET", "/cgi-bin/menu/get",
            params={"access_token": token, "agentid": self.config.agent_id},
        )
        errcode = self._errcode(data)
        if errcode in TOKEN_ERROR_CODES:
            token = await self.access_token(force_refresh=True)
            data = await self._request_json(
                "GET", "/cgi-bin/menu/get",
                params={"access_token": token, "agentid": self.config.agent_id},
            )
            errcode = self._errcode(data)
        if errcode:
            raise WeComError(
                f"企业微信读取应用菜单失败（错误码 {errcode}：{data.get('errmsg') or 'unknown'}）",
                errcode=errcode,
            )
        return data

    async def delete_menu(self) -> None:
        token = await self.access_token()
        data = await self._request_json(
            "GET", "/cgi-bin/menu/delete",
            params={"access_token": token, "agentid": self.config.agent_id},
        )
        errcode = self._errcode(data)
        if errcode in TOKEN_ERROR_CODES:
            token = await self.access_token(force_refresh=True)
            data = await self._request_json(
                "GET", "/cgi-bin/menu/delete",
                params={"access_token": token, "agentid": self.config.agent_id},
            )
            errcode = self._errcode(data)
        if errcode:
            raise WeComError(
                f"企业微信删除应用菜单失败（错误码 {errcode}：{data.get('errmsg') or 'unknown'}）",
                errcode=errcode,
            )

    async def send_text(
        self,
        title: str,
        message: str,
        *,
        recipients: tuple[str, ...] | None = None,
    ) -> None:
        token = await self.access_token()
        # An empty recipient tuple intentionally maps to @all; callers may pass
        # the configured userid whitelist for restricted notifications.
        target_recipients = recipients or ()
        payload = {
            "touser": "|".join(target_recipients) or "@all",
            "msgtype": "text",
            "agentid": self.config.agent_id,
            "text": {"content": f"{title}\n{message}"},
            "safe": 0,
            "enable_duplicate_check": 1,
            "duplicate_check_interval": 1800,
        }
        data = await self._request_json(
            "POST", "/cgi-bin/message/send", params={"access_token": token}, json=payload
        )
        errcode = self._errcode(data)
        if errcode in TOKEN_ERROR_CODES:
            token = await self.access_token(force_refresh=True)
            data = await self._request_json(
                "POST", "/cgi-bin/message/send", params={"access_token": token}, json=payload
            )
            errcode = self._errcode(data)
        invalid = "|".join(
            value
            for value in (
                str(data.get("invaliduser") or ""),
                str(data.get("unlicenseduser") or ""),
            )
            if value
        )
        if invalid and not errcode:
            logger.warning("企业微信消息部分接收人无效：%s", invalid)
        if errcode:
            detail = str(data.get("errmsg") or "unknown")
            suffix = f"，无效用户: {invalid}" if invalid else ""
            raise WeComError(f"企业微信发送消息失败（错误码 {errcode}：{detail}{suffix}）")
