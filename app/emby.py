"""Emby REST API 客户端。

对应 Emby.ApiClients / Emby.SDK 里的 UserService、SessionService 等，
这里只挑控制器真正需要的端点，用 httpx 直接实现，避免引入 .NET SDK。
所有请求都用 X-Emby-Token 头鉴权，不把 api_key 放进 query string，
以免密钥出现在 Emby 的访问日志里。
"""

import logging
from typing import Any
from uuid import uuid4

import httpx

from .logging_config import register_sensitive_values

logger = logging.getLogger(__name__)

TICKS_PER_SECOND = 10_000_000

# 关闭播放时要一起关掉的字段。转码/混流/同步也算播放路径，
# 只关 EnableMediaPlayback 仍可能通过转码端点取到流，所以收回时全关。
PLAYBACK_POLICY_FIELDS = (
    "EnableMediaPlayback",
    "EnableVideoPlaybackTranscoding",
    "EnableAudioPlaybackTranscoding",
    "EnablePlaybackRemuxing",
    "EnableMediaConversion",
    "EnableSyncTranscoding",
)

# 开放播放时只写这一个字段。转码等附加能力保持关闭 —— 用户环境走 302 直链，
# 客户端直连网盘取流，本来就用不到服务端转码。
PLAYBACK_ENABLE_FIELDS = ("EnableMediaPlayback",)

# 自助注册账户的基线策略：仅保留远程访问，其余能关的全关。
# 刻意不含 EnableAllFolders —— 媒体库可见性必须留着，否则登录后一片空白，
# 「仅能查看封面」就无从实现。
# EnableAllDevices 也刻意保持 True：它不是播放开关，而是「允许任意设备」，
# 关掉且 EnabledDevices 为空等于任何客户端都连不上，会把用户直接锁死。
RESTRICTED_POLICY: dict[str, Any] = {
    "EnableRemoteAccess": True,
    "EnableAllDevices": True,
    "IsHidden": True,
    "IsHiddenRemotely": True,
    "IsHiddenFromUnusedDevices": True,
    "IsAdministrator": False,
    "EnableContentDeletion": False,
    "EnableContentDeletionFromFolders": [],
    "EnableContentDownloading": False,
    "EnableSubtitleDownloading": False,
    "EnableSubtitleManagement": False,
    "EnablePublicSharing": False,
    "EnableLiveTvAccess": False,
    "EnableLiveTvManagement": False,
    "EnableAllChannels": False,
    "EnabledChannels": [],
    # 关掉 Emby 客户端里的「个人设置」，用户改不了自己的密码。
    # 密码只能在 portal 改，由控制器同步到 Emby —— Emby API 无法读回密码明文，
    # 若放任用户在 Emby 侧自行修改，portal 与 Emby 两边就会不一致。
    "EnableUserPreferenceAccess": False,
    "EnableRemoteControlOfOtherUsers": False,
    "EnableSharedDeviceControl": False,
    "ForceRemoteSourceTranscoding": False,
}


def playback_policy(allowed: bool) -> dict[str, Any]:
    """生成控制器默认播放策略。

    允许播放也只授予媒体播放本身；转码、混流、媒体转换和同步转码
    不会因创建或开通账号而被隐式打开。关闭播放时所有路径均关闭。
    """
    values = {field: False for field in PLAYBACK_POLICY_FIELDS}
    if allowed:
        values["EnableMediaPlayback"] = True
    return values


class EmbyError(RuntimeError):
    pass


class EmbyClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        verify_ssl: bool = True,
        timeout: float = 12.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        register_sensitive_values(self.base_url, self._api_key)

    async def __aenter__(self) -> "EmbyClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self._timeout,
            verify=self._verify_ssl,
            headers={
                "X-Emby-Token": self._api_key,
                "Accept": "application/json",
                "User-Agent": "EmbyController/1.0",
            },
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self._client is None:
            raise EmbyError("EmbyClient 必须在 async with 上下文中使用")
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise EmbyError(f"连接 {self.base_url} 失败: {exc}") from exc
        if response.status_code == 401:
            raise EmbyError("鉴权失败 (401)，请检查 API Key 是否有效")
        if response.status_code >= 400:
            raise EmbyError(
                f"{method} {path} 返回 {response.status_code}: {response.text[:200]}"
            )
        return response

    async def _get_json(self, path: str, **kwargs: Any) -> Any:
        response = await self._request("GET", path, **kwargs)
        if not response.content:
            return None
        return response.json()

    # ---- 系统 ----

    async def system_info(self) -> dict[str, Any]:
        return await self._get_json("/System/Info") or {}

    # ---- 用户 ----

    async def list_users(self) -> list[dict[str, Any]]:
        data = await self._get_json("/Users/Query")
        if isinstance(data, dict):
            return list(data.get("Items") or [])
        # 老版本 Emby 的 /Users 直接返回数组
        return list(await self._get_json("/Users") or [])

    async def get_user(self, user_id: str) -> dict[str, Any]:
        return await self._get_json(f"/Users/{user_id}") or {}

    async def list_virtual_folders(self) -> list[dict[str, Any]]:
        """读取 Emby 媒体库列表，用于管理端选择 EnabledFolders。"""
        data = await self._get_json("/Library/VirtualFolders")
        if isinstance(data, dict):
            return list(data.get("Items") or data.get("items") or [])
        return list(data or [])

    async def _patch_policy(self, user_id: str, changes: dict[str, Any]) -> None:
        """先读回完整 Policy 再改写，避免把其他策略字段清空。

        所有 Policy 写入都必须走这里：删除权限的强制关闭在此统一兜底，
        避免将来新增调用点时漏掉。
        """
        user = await self.get_user(user_id)
        policy = dict(user.get("Policy") or {})
        if not policy:
            raise EmbyError(f"用户 {user_id} 没有返回 Policy，无法安全改写")
        policy.update(changes)

        # 铁规：非管理员在任何情况下都不得拥有删除权限。
        # 即便有人在 Emby 后台手动开了，控制器下一次写 Policy 就会关回去。
        if not policy.get("IsAdministrator"):
            policy["EnableContentDeletion"] = False
            policy["EnableContentDeletionFromFolders"] = []

        await self._request("POST", f"/Users/{user_id}/Policy", json=policy)

    async def set_user_disabled(self, user_id: str, disabled: bool) -> None:
        await self._patch_policy(user_id, {"IsDisabled": disabled})

    async def set_playback_allowed(self, user_id: str, allowed: bool) -> None:
        """只开关播放相关权限，媒体库可见性不动，未激活用户仍能浏览封面。"""
        await self._patch_policy(user_id, playback_policy(allowed))

    async def set_policy_fields(self, user_id: str, changes: dict[str, Any]) -> None:
        """合并写入任意已知或版本新增的 Policy 字段。"""
        await self._patch_policy(user_id, changes)

    async def apply_restricted_policy(
        self,
        user_id: str,
        *,
        allow_playback: bool,
        allow_self_password: bool = False,
    ) -> None:
        """把账户压到最小权限：仅保留远程访问，其余全关，并隐藏用户。

        allow_self_password 打开时才把 Emby 的「个人设置」还给用户，
        代价是用户可能在 Emby 侧改密，导致 portal 密码与 Emby 不一致。
        """
        changes = dict(RESTRICTED_POLICY)
        changes.update(playback_policy(allow_playback))
        changes["EnableUserPreferenceAccess"] = allow_self_password
        await self._patch_policy(user_id, changes)

    async def set_self_password_allowed(self, user_id: str, allowed: bool) -> None:
        """单独开关 Emby 侧的个人设置入口，不动其他策略。"""
        await self._patch_policy(user_id, {"EnableUserPreferenceAccess": allowed})

    async def create_user(self, name: str) -> dict[str, Any]:
        response = await self._request("POST", "/Users/New", json={"Name": name})
        return response.json() if response.content else {}

    async def delete_user(self, user_id: str) -> None:
        await self._request("DELETE", f"/Users/{user_id}")

    async def rename_user(self, user_id: str, name: str) -> None:
        """改名要连带回传现有资料，只发 Name 会被部分版本当成清空其余字段。"""
        user = await self.get_user(user_id)
        config = dict(user.get("Configuration") or {})
        await self._request(
            "POST",
            f"/Users/{user_id}",
            json={"Name": name, "Configuration": config},
        )

    async def set_password(self, user_id: str, new_password: str) -> None:
        """管理员用 API Key 重置密码，无需知道旧密码。

        ResetPassword=false 时 Emby 需要 CurrentPw 字段存在（可为空串）。
        """
        await self._request(
            "POST",
            f"/Users/{user_id}/Password",
            json={
                "Id": user_id,
                "CurrentPw": "",
                "NewPw": new_password,
                "ResetPassword": False,
            },
        )

    async def send_message(self, session_id: str, header: str, text: str) -> None:
        await self._request(
            "POST",
            f"/Sessions/{session_id}/Message",
            json={"Header": header, "Text": text, "TimeoutMs": 8000},
        )

    # ---- 会话 ----

    async def list_sessions(self) -> list[dict[str, Any]]:
        return list(await self._get_json("/Sessions") or [])

    async def stop_playback(self, session_id: str) -> None:
        await self._request("POST", f"/Sessions/{session_id}/Playing/Stop")

    async def verify_credentials(self, username: str, password: str) -> bool:
        """用 AuthenticateByName 校验某账号的 Emby 原密码是否正确。

        与其他调用不同，这个端点必须带 X-Emby-Authorization 头声明客户端身份，
        否则 Emby 直接 400。认证成功会在服务器上留一条设备记录和一个 AccessToken，
        这里立刻吊销，避免每次认领都堆一个幽灵设备。
        """
        auth_header = (
            'MediaBrowser Client="EmbyController", Device="Controller", '
            f'DeviceId="claim-{uuid4().hex[:12]}", Version="1.0"'
        )
        try:
            response = await self._request(
                "POST",
                "/Users/AuthenticateByName",
                json={"Username": username, "Pw": password},
                headers={"X-Emby-Authorization": auth_header},
            )
        except EmbyError:
            return False

        payload: Any = None
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            device_id = str(payload.get("SessionInfo", {}).get("DeviceId") or "")
            if device_id:
                try:
                    await self.revoke_device(device_id)
                except EmbyError as exc:
                    logger.warning("认领后清理临时设备失败: %s", exc)
        return True

    # ---- 设备与令牌 ----

    async def list_devices(self, user_id: str | None = None) -> list[dict[str, Any]]:
        params = {"UserId": user_id} if user_id else None
        data = await self._get_json("/Devices", params=params)
        if isinstance(data, dict):
            return list(data.get("Items") or [])
        return list(data or [])

    async def revoke_device(self, device_id: str) -> None:
        """删除设备记录，连带吊销它持有的 access token。

        302 直链方案（embyExternalUrl / MediaLinker）下媒体流不经过 Emby，
        Playing/Stop 这类遥控指令拦不住已经拿到直链的客户端。吊销令牌能让
        它后续所有 API 请求（包括换集、拿新直链）都变成 401。
        """
        await self._request("DELETE", "/Devices", params={"Id": device_id})

    async def image_bytes(
        self, item_id: str, image_type: str = "Primary", max_height: int = 220
    ) -> tuple[bytes, str] | None:
        try:
            response = await self._request(
                "GET",
                f"/Items/{item_id}/Images/{image_type}",
                params={"maxHeight": max_height, "quality": 80},
            )
        except EmbyError:
            return None
        content_type = response.headers.get("content-type", "image/jpeg")
        return response.content, content_type


def ticks_to_seconds(ticks: int | None) -> float:
    return round((ticks or 0) / TICKS_PER_SECOND, 2)


def format_ticks(ticks: int | None) -> str:
    total = int(ticks_to_seconds(ticks))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def progress_percent(position_ticks: int | None, runtime_ticks: int | None) -> float:
    runtime = runtime_ticks or 0
    if runtime <= 0:
        return 0.0
    return round(min(100.0, max(0.0, (position_ticks or 0) / runtime * 100)), 1)


def session_display_title(session: dict[str, Any]) -> tuple[str, str | None]:
    """返回 (标题, 剧集名)。剧集用 SeriesName 兜底展示。"""
    item = session.get("NowPlayingItem") or {}
    name = item.get("Name") or ""
    series = item.get("SeriesName")
    if series and item.get("IndexNumber") is not None:
        season = item.get("ParentIndexNumber")
        prefix = f"S{season:02d}E{item['IndexNumber']:02d}" if season is not None else f"E{item['IndexNumber']:02d}"
        name = f"{prefix} {name}".strip()
    return name, series
