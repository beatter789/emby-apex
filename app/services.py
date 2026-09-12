"""业务逻辑层：同步用户、主动轮询播放会话、强制客户端规则、到期处理。"""

import fnmatch
import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import case, delete, func, inspect, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from . import notify, settings_store
from .config import get_settings
from .emby import (
    EmbyClient,
    EmbyError,
    PLAYBACK_POLICY_FIELDS,
    RESTRICTED_POLICY,
    format_ticks,
    playback_policy,
    progress_percent,
    session_display_title,
    ticks_to_seconds,
)
from .models import (
    ActionLog,
    BillCodeDailyUsage,
    BillCodeUsage,
    ManagedUser,
    MediaRequest,
    MediaRequestSummary,
    PlaybackRecord,
    RedeemCode,
    Server,
    utcnow,
)
from .security import decrypt_secret, hash_password, verify_password
from .tmdb import TmdbClient, TmdbError
from .moviepilot import MoviePilotClient, MoviePilotError

logger = logging.getLogger(__name__)
_settings = get_settings()

_PLAYBACK_PROCESS_ID = uuid4().hex[:12]
_playback_lock = asyncio.Lock()
_user_sync_lock = asyncio.Lock()
_user_mutation_lock = asyncio.Lock()

# TMDB 详情在搜索结果中通常已经完整返回；该缓存主要覆盖直接打开
# TMDB ID 链接，避免同一作品在短时间内反复建立 HTTP 连接。
_TMDB_DETAILS_TTL = 60.0
_tmdb_details_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_moviepilot_exists_cache: dict[tuple[str, str, str], tuple[float, bool | None]] = {}
_moviepilot_exists_sem = asyncio.Semaphore(4)

# 海报和通知不能阻塞“确认入库”这个关键操作。按服务器/作品去重，避免
# 用户连续点击或多名管理员同时确认时启动重复下载。
_poster_tasks: set[tuple[int, str, int]] = set()
_background_tasks: set[asyncio.Task[Any]] = set()


def clear_tmdb_cache() -> None:
    """运行期 Key/代理变更后丢弃旧详情，确保下一次请求使用新配置。"""
    _tmdb_details_cache.clear()


async def wait_background_tasks(timeout: float = 6.0) -> None:
    """优雅退出时尽量完成已确认作品的海报和通知任务。"""
    tasks = tuple(task for task in _background_tasks if not task.done())
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=max(0.1, timeout))
    for task in pending:
        task.cancel()
    if done or pending:
        await asyncio.gather(*done, *pending, return_exceptions=True)


@dataclass
class _PlaybackAccumulator:
    session_key: str
    server_id: int
    emby_user_id: str
    username: str
    item_id: str | None
    item_name: str
    series_name: str | None
    item_type: str | None
    client: str
    device_name: str
    device_id: str | None
    remote_ip: str | None
    play_method: str | None
    position_ticks: int
    runtime_ticks: int
    watched_seconds: float
    started_at: datetime
    last_seen_at: datetime


_playback_cache: dict[tuple[int, str, str], _PlaybackAccumulator] = {}
_playback_generations: dict[tuple[int, str, str], int] = {}
_deleted_playback_users: set[tuple[int, str]] = set()


def playback_user_deleted(server_id: int, emby_user_id: str) -> bool:
    return (server_id, emby_user_id) in _deleted_playback_users

# 老账号认领要拿用户填的密码去问 Emby，等于在公开端点上开了一个在线验密入口。
# 按用户名限流，避免被拿来撞库。单进程内存计数，够用且不引依赖。
CLAIM_MAX_ATTEMPTS = 5
CLAIM_ATTEMPT_WINDOW = timedelta(minutes=15)
_claim_attempts: dict[str, list[datetime]] = {}

# 账单码规则与每日额度。日期段是账单码的第 11-18 位（1 起算）。
BILL_CODE_PREFIX = "1000"  # 四位前缀，不能误写为五位 "10000"
BILL_CODE_MIN_LENGTH = 30
BILL_CODE_MAX_LENGTH = 32
BILL_CODE_DAILY_LIMIT = 3
ACTIVATION_CODE_MAX_FAILURES = 5
BILL_CODE_INVALID_MESSAGE = "账单码无效，请检查后重试"
ACTIVATION_UNAVAILABLE_MESSAGE = "账户已停用，账单码激活功能不可用"


def bill_code_today() -> str:
    """Return today's date in the configured application timezone."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(_settings.timezone)).strftime("%Y%m%d")


def validate_bill_code(raw_code: str) -> str:
    """Validate an Emby Apex bill code without exposing which check failed."""
    import re

    code = str(raw_code or "")
    if not (BILL_CODE_MIN_LENGTH <= len(code) <= BILL_CODE_MAX_LENGTH):
        raise RegistrationError(BILL_CODE_INVALID_MESSAGE)
    if re.fullmatch(r"[0-9]+", code, flags=re.ASCII) is None:
        raise RegistrationError(BILL_CODE_INVALID_MESSAGE)
    if not code.startswith(BILL_CODE_PREFIX):
        raise RegistrationError(BILL_CODE_INVALID_MESSAGE)
    # The documented prefix is exactly four digits ``1000``; a fifth zero
    # would be the legacy/invalid ``10000`` form and is rejected explicitly.
    if code.startswith("10000"):
        raise RegistrationError(BILL_CODE_INVALID_MESSAGE)
    date_text = code[10:18]
    try:
        parsed = datetime.strptime(date_text, "%Y%m%d").date()
    except ValueError as exc:
        raise RegistrationError(BILL_CODE_INVALID_MESSAGE) from exc
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo(_settings.timezone)).date()
    if parsed != today:
        raise RegistrationError(BILL_CODE_INVALID_MESSAGE)
    return code


async def _record_activation_failure(
    db: AsyncSession,
    user: ManagedUser,
    *,
    user_id: int | None = None,
) -> tuple[bool, bool]:
    """Record one failed activation attempt with an atomic, capped update."""
    if user_id is None:
        identity = inspect(user).identity
        user_id = int(identity[0]) if identity else int(user.id)
    else:
        user_id = int(user_id)
    is_admin = bool(await db.scalar(select(ManagedUser.is_admin).where(ManagedUser.id == user_id)))
    if is_admin:
        return False, False

    attempts = func.coalesce(ManagedUser.activation_failed_attempts, 0)
    next_attempts = case(
        (attempts < ACTIVATION_CODE_MAX_FAILURES, attempts + 1),
        else_=ACTIVATION_CODE_MAX_FAILURES,
    )
    reaches_limit = attempts + 1 >= ACTIVATION_CODE_MAX_FAILURES
    now = utcnow()
    result = await db.execute(
        update(ManagedUser)
        .where(
            ManagedUser.id == user_id,
            ManagedUser.is_admin.is_(False),
            ManagedUser.activation_locked.is_(False),
        )
        .values(
            activation_failed_attempts=next_attempts,
            activation_locked=case((reaches_limit, True), else_=ManagedUser.activation_locked),
            activation_locked_at=case((reaches_limit, now), else_=ManagedUser.activation_locked_at),
            is_disabled=case((reaches_limit, True), else_=ManagedUser.is_disabled),
            disabled_by_controller=case((reaches_limit, True), else_=ManagedUser.disabled_by_controller),
            playback_enabled=case((reaches_limit, False), else_=ManagedUser.playback_enabled),
        )
    )
    await db.refresh(user)
    await db.commit()
    locked = bool(user.activation_locked)
    # A rowcount of one means this request performed the atomic update. If it
    # is the update that reached the cap, keep this request's response generic;
    # only later requests should reveal that the account is unavailable.
    newly_locked = bool(
        result.rowcount == 1
        and locked
        and int(user.activation_failed_attempts or 0) >= ACTIVATION_CODE_MAX_FAILURES
    )
    return locked, newly_locked


async def bill_code_usage_today(db: AsyncSession, user_id: int) -> int:
    row = await db.scalar(
        select(BillCodeDailyUsage).where(
            BillCodeDailyUsage.user_id == user_id,
            BillCodeDailyUsage.usage_date == bill_code_today(),
        )
    )
    return int(row.used_count) if row else 0


async def _bill_code_failure(
    db: AsyncSession, user: ManagedUser, *, user_id: int | None = None
) -> None:
    """Count a bill-code failure and expose only the post-lock state."""
    locked, newly_locked = await _record_activation_failure(db, user, user_id=user_id)
    if locked and not newly_locked:
        raise RegistrationError(ACTIVATION_UNAVAILABLE_MESSAGE)
    raise RegistrationError(BILL_CODE_INVALID_MESSAGE)


async def create_redeem_code_from_bill(
    db: AsyncSession, user: ManagedUser, raw_bill_code: str
) -> tuple[RedeemCode, int]:
    """Consume a bill code once and issue a user-owned code for a 30-day entitlement.

    The daily upsert's ``WHERE used_count < 3`` predicate is atomic in
    SQLite, so concurrent requests cannot exceed the per-user limit. Every
    bill-code validation or usability failure is counted by the same atomic
    failure ledger.
    """
    identity = inspect(user).identity
    user_id = int(identity[0]) if identity else int(user.id)
    if user.activation_locked or (user.is_disabled and not user.is_admin):
        raise RegistrationError(ACTIVATION_UNAVAILABLE_MESSAGE)
    try:
        bill_code = validate_bill_code(raw_bill_code)
    except RegistrationError:
        await _bill_code_failure(db, user, user_id=user_id)
    existing = await db.scalar(
        select(BillCodeUsage).where(BillCodeUsage.bill_code == bill_code)
    )
    if existing is not None:
        await _bill_code_failure(db, user, user_id=user_id)

    usage_date = bill_code_today()
    stmt = sqlite_insert(BillCodeDailyUsage).values(
        user_id=user.id, usage_date=usage_date, used_count=1
    ).on_conflict_do_update(
        index_elements=[BillCodeDailyUsage.user_id, BillCodeDailyUsage.usage_date],
        set_={"used_count": BillCodeDailyUsage.used_count + 1},
        where=BillCodeDailyUsage.used_count < BILL_CODE_DAILY_LIMIT,
    )
    result = await db.execute(stmt)
    if result.rowcount != 1:
        await db.rollback()
        await _bill_code_failure(db, user, user_id=user_id)

    now = utcnow()
    redeem_code_text = ""
    for _ in range(20):
        candidate = _new_code()
        if await db.scalar(select(RedeemCode.id).where(RedeemCode.code == candidate)) is None:
            redeem_code_text = candidate
            break
    if not redeem_code_text:
        await db.rollback()
        raise RegistrationError("生成兑换码失败，请稍后重试")
    redeem = RedeemCode(
        code=redeem_code_text,
        duration_seconds=30 * 24 * 3600,
        amount=30,
        unit="day",
        owner_user_id=user.id,
        # 用户生成的待用码永不过期；兑换时才将 30 天时长加到账户。
        expires_at=None,
        source_bill_code=bill_code,
        note="账单码自动生成",
    )
    db.add(redeem)
    try:
        await db.flush()
        db.add(BillCodeUsage(bill_code=bill_code, user_id=user.id, redeem_code_id=redeem.id, used_at=now))
        await log_action(
            db,
            "bill_code_redeemed",
            "用户账单码提交成功，已生成待使用兑换码",
            username=user.username,
            commit=False,
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # A concurrent request may have committed the same bill code.  The
        # unique constraint is authoritative and the rolled-back quota update
        # does not consume a daily attempt.
        duplicate = await db.scalar(
            select(BillCodeUsage).where(BillCodeUsage.bill_code == bill_code)
        )
        if duplicate is not None:
            await _bill_code_failure(db, user, user_id=user_id)
        raise RegistrationError("生成兑换码失败，请稍后重试") from exc
    used_today = await bill_code_usage_today(db, user.id)
    try:
        await notify.emit_activation_success(
            {
                "username": user.username,
                "activated_at": now.isoformat(timespec="seconds"),
                "bill_code": bill_code,
                "redeem_code": redeem.code,
            }
        )
    except Exception as exc:
        # Notification failure must never roll back the already-issued code;
        # only the exception type is safe to write to logs.
        logger.warning("激活成功通知失败 error_type=%s", type(exc).__name__)
    return redeem, used_today


async def _safe_notify(
    title: str, message: str, *, category: str = "general"
) -> None:
    """通知是提交后的副作用，任何异常都不得影响主流程。"""
    try:
        await notify.push(title, message, category=category)
    except Exception as exc:
        # 只记录类型，不记录异常文本或消息内容，避免第三方 URL/凭据进入日志。
        logger.warning(
            "通知发送失败 category=%s error_type=%s", category, type(exc).__name__
        )


def policy_for_user(user: ManagedUser) -> dict[str, Any]:
    """读取本地 Policy 快照；旧记录或坏 JSON 返回空字典。"""
    try:
        value = json.loads(user.policy_json or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _dump_policy(policy: dict[str, Any]) -> str:
    return json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def libraries_for_server(server: Server) -> list[dict[str, str]]:
    try:
        value = json.loads(server.library_json or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [
        {"id": str(item.get("id") or ""), "name": str(item.get("name") or "")}
        for item in value
        if isinstance(item, dict) and item.get("id")
    ]


def _claim_rate_limited(username: str) -> bool:
    """记录一次认领尝试，返回是否已超出窗口内允许的次数。"""
    key = username.lower()
    now = utcnow()
    recent = [
        stamp for stamp in _claim_attempts.get(key, []) if now - stamp < CLAIM_ATTEMPT_WINDOW
    ]
    if len(recent) >= CLAIM_MAX_ATTEMPTS:
        _claim_attempts[key] = recent
        return True
    recent.append(now)
    _claim_attempts[key] = recent
    return False


def _claim_attempts_reset(username: str) -> None:
    _claim_attempts.pop(username.lower(), None)


def client_for(server: Server) -> EmbyClient:
    return EmbyClient(
        server.base_url,
        decrypt_secret(server.api_key_encrypted),
        verify_ssl=server.verify_ssl,
        timeout=_settings.http_timeout_seconds,
    )


async def log_action(
    db: AsyncSession,
    action: str,
    detail: str,
    *,
    level: str = "info",
    server_name: str | None = None,
    username: str | None = None,
    commit: bool = True,
) -> None:
    db.add(
        ActionLog(
            action=action,
            detail=detail,
            level=level,
            server_name=server_name,
            username=username,
        )
    )
    if commit:
        await db.commit()


# --------------------------------------------------------------------------
# 用户同步
# --------------------------------------------------------------------------


async def sync_server_users(db: AsyncSession, server: Server) -> int:
    async with _user_mutation_lock:
        return await _sync_server_users(db, server)


async def _sync_server_users(db: AsyncSession, server: Server) -> int:
    """批量同步用户、完整 Policy 和媒体库快照，尽量避免无变化写入。"""
    now = utcnow()
    async with client_for(server) as client:
        info = await client.system_info()
        emby_users = await client.list_users()
        try:
            virtual_folders = await client.list_virtual_folders()
        except EmbyError as exc:
            virtual_folders = None
            logger.warning("读取 %s 媒体库失败，保留旧快照：%s", server.name, exc)

        existing = {
            user.emby_user_id: user
            for user in (
                await db.scalars(
                    select(ManagedUser).where(ManagedUser.server_id == server.id)
                )
            ).all()
        }

        if virtual_folders is not None:
            libraries = [
                {
                    "id": str(item.get("ItemId") or item.get("Id") or ""),
                    "name": str(item.get("Name") or ""),
                }
                for item in virtual_folders
                if item.get("ItemId") or item.get("Id")
            ]
            encoded = json.dumps(libraries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if server.library_json != encoded:
                server.library_json = encoded

        seen: set[str] = set()
        for entry in emby_users:
            user_id = str(entry.get("Id") or "")
            if not user_id:
                continue
            seen.add(user_id)
            _deleted_playback_users.discard((server.id, user_id))
            policy = dict(entry.get("Policy") or {})
            record = existing.get(user_id)
            if record is None:
                record = ManagedUser(
                    server_id=server.id,
                    emby_user_id=user_id,
                    username="",
                    playback_source="emby",
                )
                prior_seconds = await db.scalar(
                    select(func.coalesce(func.sum(PlaybackRecord.watched_seconds), 0)).where(
                        PlaybackRecord.server_id == server.id,
                        PlaybackRecord.emby_user_id == user_id,
                    )
                )
                prior_last = await db.scalar(
                    select(func.max(PlaybackRecord.started_at)).where(
                        PlaybackRecord.server_id == server.id,
                        PlaybackRecord.emby_user_id == user_id,
                    )
                )
                record.total_playback_seconds = float(prior_seconds or 0)
                record.last_played_at = prior_last
                db.add(record)
            elif record.playback_source not in {"controller", "emby"}:
                record.playback_source = "controller" if record.self_registered else "emby"

            old_snapshot = record.policy_json
            old_username = record.username
            old_disabled = record.is_disabled
            old_playback = record.playback_enabled
            old_activity = record.last_activity_at
            record.username = entry.get("Name") or record.username
            record.is_admin = bool(policy.get("IsAdministrator"))
            if record.is_admin:
                record.is_protected = True

            remote_disabled = bool(policy.get("IsDisabled"))
            if record.activation_locked and not record.is_admin:
                # A locked account must stay disabled even if a remote sync
                # sees stale/externally changed Policy data.
                if not remote_disabled:
                    try:
                        await client.set_user_disabled(record.emby_user_id, True)
                        policy["IsDisabled"] = True
                    except EmbyError as exc:
                        logger.warning(
                            "保持锁定账户停用状态失败 server=%s user=%s error_type=%s",
                            server.name,
                            record.username,
                            type(exc).__name__,
                        )
                record.is_disabled = True
            else:
                record.is_disabled = remote_disabled

            remote_playback = bool(policy.get("EnableMediaPlayback"))
            if record.playback_source == "controller" and not record.is_admin:
                desired_playback = bool(record.playback_enabled)
                playback_drifted = remote_playback != desired_playback
                if not desired_playback:
                    playback_drifted = any(
                        bool(policy.get(field)) for field in PLAYBACK_POLICY_FIELDS
                    )
                if playback_drifted:
                    try:
                        await client.set_playback_allowed(record.emby_user_id, desired_playback)
                        policy.update(playback_policy(desired_playback))
                        remote_playback = desired_playback
                    except EmbyError as exc:
                        logger.warning("回正 %s 播放权限失败：%s", record.username, exc)
                record.playback_enabled = desired_playback
            else:
                record.playback_enabled = remote_playback
            record.policy_json = _dump_policy(policy)

            last_activity = entry.get("LastActivityDate")
            if last_activity:
                parsed_activity = _parse_emby_datetime(last_activity)
                if parsed_activity is not None:
                    record.last_activity_at = parsed_activity
            changed = (
                old_snapshot != record.policy_json
                or old_username != record.username
                or old_disabled != record.is_disabled
                or old_playback != record.playback_enabled
                or old_activity != record.last_activity_at
            )
            previous_sync = as_utc(record.synced_at)
            if changed or previous_sync is None or now - previous_sync >= timedelta(minutes=1):
                record.synced_at = now

        # Emby 上已删除的用户，本地影子记录也清理掉（只删本地行，不回头动 Emby）。
        for user_id, record in existing.items():
            if user_id not in seen:
                await _delete_local_user_data(db, record, server)

    server.server_version = info.get("Version") or server.server_version
    previous_ok = as_utc(server.last_ok_at)
    if previous_ok is None or now - previous_ok >= timedelta(minutes=1):
        server.last_ok_at = now
    server.last_error = None
    await db.commit()
    return len(seen)


async def sync_all_servers(db: AsyncSession) -> dict[str, str]:
    if _user_sync_lock.locked():
        return {"全量同步": "已有同步正在进行，本次请求已合并"}
    results: dict[str, str] = {}
    async with _user_sync_lock:
        servers = (
            await db.scalars(select(Server).where(Server.enabled.is_(True)))
        ).all()
        for server in servers:
            server_id = server.id
            server_name = server.name
            try:
                count = await sync_server_users(db, server)
                results[server_name] = f"同步 {count} 个用户"
            except (EmbyError, ValueError) as exc:
                await db.rollback()
                failed_server = await db.get(Server, server_id)
                if failed_server is not None and failed_server.last_error != str(exc):
                    failed_server.last_error = str(exc)
                    await db.commit()
                results[server_name] = f"失败: {exc}"
                logger.warning("同步 %s 失败: %s", server_name, exc)
    return results


def _parse_emby_datetime(value: str) -> datetime | None:
    text = value.replace("Z", "+00:00")
    # Emby 的时间戳小数位可能超过 6 位，fromisoformat 处理不了
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(ch for ch in tail if ch.isdigit())[:6]
        offset = tail[len(digits):] if len(tail) > len(digits) else ""
        offset = offset.lstrip("0123456789")
        text = f"{head}.{digits}{offset}" if digits else f"{head}{offset}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# 正在播放
# --------------------------------------------------------------------------


@dataclass
class LiveSession:
    server_id: int
    server_name: str
    session_id: str
    emby_user_id: str
    username: str
    item_id: str | None
    title: str
    series_name: str | None
    item_type: str | None
    client: str
    device_name: str
    device_id: str | None
    remote_ip: str | None
    play_method: str | None
    is_paused: bool
    position_label: str
    runtime_label: str
    percent: float
    blocked_reason: str | None = None


@dataclass
class SessionPollResult:
    """主动轮询一次的结果及缓存更新所需的服务器状态。"""

    live: list[LiveSession]
    successful_server_ids: set[int]
    enabled_server_ids: set[int]


def _extract_sessions(server: Server, raw_sessions: list[dict[str, Any]]) -> list[LiveSession]:
    live: list[LiveSession] = []
    for session in raw_sessions:
        item = session.get("NowPlayingItem")
        if not item:
            continue
        play_state = session.get("PlayState") or {}
        title, series = session_display_title(session)
        live.append(
            LiveSession(
                server_id=server.id,
                server_name=server.name,
                session_id=session.get("Id") or "",
                emby_user_id=session.get("UserId") or "",
                username=session.get("UserName") or "未知",
                item_id=item.get("Id"),
                title=title,
                series_name=series,
                item_type=item.get("Type"),
                client=session.get("Client") or "",
                device_name=session.get("DeviceName") or "",
                device_id=session.get("DeviceId"),
                remote_ip=session.get("RemoteEndPoint"),
                play_method=play_state.get("PlayMethod"),
                is_paused=bool(play_state.get("IsPaused")),
                position_label=format_ticks(play_state.get("PositionTicks")),
                runtime_label=format_ticks(item.get("RunTimeTicks")),
                percent=progress_percent(
                    play_state.get("PositionTicks"), item.get("RunTimeTicks")
                ),
            )
        )
    return live


def _webhook_value(value: Any, *names: str) -> Any:
    """Read Emby webhook fields while accepting PascalCase and camelCase."""
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


def _webhook_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def webhook_event_kind(payload: dict[str, Any]) -> str:
    """Classify common Emby playback notification names."""
    raw = _webhook_value(payload, "Event", "NotificationType", "event", "type")
    compact = "".join(ch for ch in str(raw or "").casefold() if ch.isalnum())
    if any(marker in compact for marker in ("playbackstop", "playbackend", "playbackended")):
        return "stop"
    if "playbackpause" in compact:
        return "pause"
    if any(marker in compact for marker in ("playbackunpause", "playbackresume", "playbackresumed")):
        return "resume"
    if "playbackstart" in compact:
        return "start"
    if "playbackprogress" in compact or "playbacktime" in compact:
        return "progress"
    # Some webhook providers omit the event name but still provide a session.
    if _webhook_value(payload, "Session", "session", "SessionId", "sessionId") is not None:
        return "progress"
    return "ignored"


def normalize_webhook_session(payload: dict[str, Any], *, kind: str = "progress") -> dict[str, Any]:
    """Convert Emby webhook payload variants into the existing session shape."""
    nested_session = _webhook_dict(_webhook_value(payload, "Session", "session"))
    nested_item = _webhook_dict(
        _webhook_value(payload, "Item", "NowPlayingItem", "item", "nowPlayingItem")
        or _webhook_value(nested_session, "NowPlayingItem", "Item", "item")
    )
    if not nested_item:
        # Older Emby webhook providers flatten item fields at the top level.
        flat_item_id = _webhook_value(payload, "ItemId", "item_id")
        flat_item_name = _webhook_value(payload, "ItemName", "Title", "Name", "item_name")
        if flat_item_id or flat_item_name:
            nested_item = {
                "Id": flat_item_id,
                "Name": flat_item_name,
                "Type": _webhook_value(payload, "ItemType", "Type", "item_type"),
                "SeriesName": _webhook_value(payload, "SeriesName", "series_name"),
                "RunTimeTicks": _webhook_value(payload, "RunTimeTicks", "runtime_ticks"),
            }
    nested_user = _webhook_dict(_webhook_value(payload, "User", "user"))
    playback = _webhook_dict(
        _webhook_value(payload, "PlaybackInfo", "PlayState", "playbackInfo", "playState")
        or _webhook_value(nested_session, "PlayState", "PlaybackInfo", "playState")
    )
    if not nested_item:
        flat_item_id = _webhook_value(playback, "ItemId", "item_id")
        flat_item_name = _webhook_value(playback, "ItemName", "Title", "Name", "item_name")
        if flat_item_id or flat_item_name:
            nested_item = {
                "Id": flat_item_id,
                "Name": flat_item_name,
                "Type": _webhook_value(playback, "ItemType", "Type", "item_type"),
                "SeriesName": _webhook_value(playback, "SeriesName", "series_name"),
                "RunTimeTicks": _webhook_value(playback, "RunTimeTicks", "runtime_ticks"),
            }

    def first(*sources: dict[str, Any], names: tuple[str, ...]) -> Any:
        for source in sources:
            value = _webhook_value(source, *names)
            if value is not None:
                return value
        return None

    session_id = first(
        nested_session,
        payload,
        names=("Id", "SessionId", "session_id", "id"),
    )
    user_id = _webhook_value(nested_user, "Id", "UserId", "UserID", "user_id")
    if user_id is None:
        user_id = first(
            nested_session,
            payload,
            names=("UserId", "UserID", "user_id"),
        )
    username = first(
        nested_user,
        nested_session,
        payload,
        names=("Name", "UserName", "username"),
    )
    position = first(
        playback,
        payload,
        names=("PositionTicks", "PlaybackPositionTicks", "position_ticks"),
    )
    is_paused = first(playback, payload, names=("IsPaused", "is_paused"))
    if kind == "pause":
        is_paused = True
    elif kind == "resume":
        is_paused = False
    if is_paused is None:
        is_paused = False

    item = dict(nested_item)
    if first(playback, payload, names=("RunTimeTicks", "runtime_ticks")) is not None:
        item.setdefault("RunTimeTicks", first(playback, payload, names=("RunTimeTicks", "runtime_ticks")))
    return {
        "Id": str(session_id or ""),
        "UserId": str(user_id or ""),
        "UserName": str(username or "未知"),
        "Client": first(nested_session, payload, names=("Client", "client")) or "",
        "DeviceName": first(nested_session, payload, names=("DeviceName", "device_name")) or "",
        "DeviceId": first(nested_session, payload, names=("DeviceId", "device_id")),
        "RemoteEndPoint": first(
            nested_session,
            payload,
            names=("RemoteEndPoint", "RemoteIP", "remote_ip"),
        ),
        "NowPlayingItem": item,
        "PlayState": {
            "PositionTicks": position or 0,
            "IsPaused": bool(is_paused),
            "PlayMethod": first(playback, payload, names=("PlayMethod", "play_method")),
        },
    }


async def _record_playback(
    db: AsyncSession, server: Server, session: dict[str, Any]
) -> None:
    """把播放会话聚合到内存，避免每次轮询都 UPDATE SQLite。"""
    item = session.get("NowPlayingItem") or {}
    play_state = session.get("PlayState") or {}
    session_id = session.get("Id")
    if not session_id or playback_user_deleted(server.id, str(session.get("UserId") or "")):
        return

    base_key = (server.id, str(session_id), str(item.get("Id") or "unknown"))
    title, series = session_display_title(session)
    now = utcnow()
    record = _playback_cache.get(base_key)
    if record is None:
        generation = _playback_generations.get(base_key, 0) + 1
        _playback_generations[base_key] = generation
        record = _PlaybackAccumulator(
            session_key=f"{_PLAYBACK_PROCESS_ID}:{server.id}:{session_id}:{generation}",
            server_id=server.id,
            emby_user_id=session.get("UserId") or "",
            username=session.get("UserName") or "未知",
            item_id=item.get("Id"),
            item_name=title,
            series_name=series,
            item_type=item.get("Type"),
            client=session.get("Client") or "",
            device_name=session.get("DeviceName") or "",
            device_id=session.get("DeviceId"),
            remote_ip=session.get("RemoteEndPoint"),
            play_method=play_state.get("PlayMethod"),
            position_ticks=play_state.get("PositionTicks") or 0,
            runtime_ticks=item.get("RunTimeTicks") or 0,
            watched_seconds=0.0,
            started_at=now,
            last_seen_at=now,
        )
        _playback_cache[base_key] = record
        return

    # 累加实际观看时长：只在未暂停且进度前进时计入
    new_position = play_state.get("PositionTicks") or 0
    if not play_state.get("IsPaused") and new_position > record.position_ticks:
        advanced = ticks_to_seconds(new_position - record.position_ticks)
        gap = (now - record.last_seen_at).total_seconds()
        # 拖进度条不算观看时长，取两者较小值
        record.watched_seconds += min(advanced, max(gap, 0))
    record.position_ticks = new_position
    record.runtime_ticks = item.get("RunTimeTicks") or record.runtime_ticks
    record.play_method = play_state.get("PlayMethod") or record.play_method
    record.last_seen_at = now


async def _flush_playback_entries(
    db: AsyncSession, entries: list[_PlaybackAccumulator], *, ended_at: datetime | None = None
) -> int:
    """一次性把内存聚合结果写入历史表。"""
    entries = [entry for entry in entries if not playback_user_deleted(entry.server_id, entry.emby_user_id)]
    for entry in entries:
        db.add(
            PlaybackRecord(
                server_id=entry.server_id,
                session_key=entry.session_key,
                emby_user_id=entry.emby_user_id,
                username=entry.username,
                item_id=entry.item_id,
                item_name=entry.item_name,
                series_name=entry.series_name,
                item_type=entry.item_type,
                client=entry.client,
                device_name=entry.device_name,
                device_id=entry.device_id,
                remote_ip=entry.remote_ip,
                play_method=entry.play_method,
                position_ticks=entry.position_ticks,
                runtime_ticks=entry.runtime_ticks,
                watched_seconds=entry.watched_seconds,
                started_at=entry.started_at,
                last_seen_at=entry.last_seen_at,
                ended_at=ended_at or entry.last_seen_at,
            )
        )
        if hasattr(db, "execute"):
            await db.execute(
                update(ManagedUser)
                .where(ManagedUser.server_id == entry.server_id,
                       ManagedUser.emby_user_id == entry.emby_user_id)
                .values(
                    total_playback_seconds=ManagedUser.total_playback_seconds + entry.watched_seconds,
                    last_played_at=case(
                        (or_(ManagedUser.last_played_at.is_(None),
                             ManagedUser.last_played_at < entry.started_at), entry.started_at),
                        else_=ManagedUser.last_played_at,
                    ),
                )
                .execution_options(synchronize_session=False)
            )
    if entries:
        await db.commit()
    return len(entries)


async def poll_sessions_by_server(db: AsyncSession) -> SessionPollResult:
    """主动从每台启用的 Emby 获取会话并累计播放历史。"""
    live: list[LiveSession] = []
    servers = (await db.scalars(select(Server).where(Server.enabled.is_(True)))).all()
    enabled_server_ids = {server.id for server in servers}
    successful_server_ids: set[int] = set()

    async with _playback_lock:
        for server in servers:
            try:
                async with client_for(server) as client:
                    raw_sessions = await client.list_sessions()
                    now = utcnow()
                    if server.last_ok_at is None or now - as_utc(server.last_ok_at) >= timedelta(minutes=5):
                        server.last_ok_at = now
                    if server.last_error is not None:
                        server.last_error = None
                    successful_server_ids.add(server.id)

                    playing = [s for s in raw_sessions if s.get("NowPlayingItem")
                               and not playback_user_deleted(server.id, str(s.get("UserId") or ""))]
                    seen_keys: set[tuple[int, str, str]] = set()
                    for session in playing:
                        item = session.get("NowPlayingItem") or {}
                        session_id = session.get("Id")
                        if session_id:
                            seen_keys.add(
                                (server.id, str(session_id), str(item.get("Id") or "unknown"))
                            )
                        await _record_playback(db, server, session)

                    # A successful poll with no longer-playing sessions is an
                    # explicit end signal for this server.
                    ended_keys = [
                        key
                        for key in list(_playback_cache)
                        if key[0] == server.id and key not in seen_keys
                    ]
                    ended = [_playback_cache.pop(key) for key in ended_keys]
                    await _flush_playback_entries(db, ended, ended_at=now)

                    server_live = _extract_sessions(server, playing)
                    for entry in server_live:
                        reason = await _enforce_client_policy(db, client, server, entry)
                        entry.blocked_reason = reason
                    live.extend(server_live)
            except (EmbyError, ValueError) as exc:
                server.last_error = str(exc)
                logger.warning("轮询 %s 失败: %s", server.name, exc)

        if db.dirty or db.new or db.deleted:
            await db.commit()
    return SessionPollResult(
        live=live,
        successful_server_ids=successful_server_ids,
        enabled_server_ids=enabled_server_ids,
    )


async def poll_sessions(db: AsyncSession) -> list[LiveSession]:
    """兼容旧的人工轮询入口，只返回当前正在播放的会话。"""
    return (await poll_sessions_by_server(db)).live


async def process_webhook_event(
    db: AsyncSession,
    server: Server,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """兼容旧的直接调用方，应用一个 Emby 播放事件到内存聚合器。

    管理端 Webhook 入口不再调用此函数，生产环境以主动轮询为唯一播放来源。
    播放开始/进度不会立即写入数据库，停止事件（或优雅退出）才是持久化边界。
    """
    kind = webhook_event_kind(payload)
    if kind == "ignored":
        return {"kind": kind, "session": None, "ended_keys": []}

    session = normalize_webhook_session(payload, kind=kind)
    session_id = str(session.get("Id") or "")
    item = session.get("NowPlayingItem") or {}
    item_id = str(item.get("Id") or "")
    if not session_id:
        return {"kind": "ignored", "session": None, "ended_keys": []}

    async with _playback_lock:
        if playback_user_deleted(server.id, str(session.get("UserId") or "")):
            return {"kind": "ignored", "session": None, "ended_keys": []}
        if kind == "stop":
            matching = [
                key
                for key in list(_playback_cache)
                if key[0] == server.id
                and key[1] == session_id
                and (not item_id or key[2] == item_id)
            ]
            entries = [_playback_cache.pop(key) for key in matching]
            await _flush_playback_entries(db, entries, ended_at=utcnow())
            return {
                "kind": kind,
                "session": None,
                "ended_keys": matching,
                "session_id": session_id,
                "item_id": item_id or None,
            }

        if not item:
            return {"kind": "ignored", "session": None, "ended_keys": []}
        await _record_playback(db, server, session)
        entries = _extract_sessions(server, [session])
        entry = entries[0] if entries else None

        # Client restrictions are enforced once at playback start.  Progress
        # webhooks only update the accumulator and never issue repeated stops.
        if entry is not None and kind == "start":
            user = await db.scalar(
                select(ManagedUser).where(
                    ManagedUser.server_id == server.id,
                    ManagedUser.emby_user_id == entry.emby_user_id,
                )
            )
            if user is not None and user.client_policy_mode != "off":
                try:
                    async with client_for(server) as client:
                        entry.blocked_reason = await _enforce_client_policy(
                            db, client, server, entry
                        )
                except EmbyError as exc:
                    logger.warning(
                        "Webhook 客户端策略执行失败 server=%s error_type=%s",
                        server.name,
                        type(exc).__name__,
                    )
                if db.dirty or db.new or db.deleted:
                    await db.commit()

        return {
            "kind": kind,
            "session": entry,
            "ended_keys": [],
            "session_id": session_id,
            "item_id": item_id or None,
        }


async def flush_playback_cache(db: AsyncSession) -> int:
    """正常退出时结束并刷入所有内存播放会话。"""
    async with _playback_lock:
        entries = list(_playback_cache.values())
        _playback_cache.clear()
        return await _flush_playback_entries(db, entries, ended_at=utcnow())


async def reset_open_playback_records(db: AsyncSession) -> int:
    """重启不恢复旧内存会话，将数据库中遗留的未结束记录收尾。"""
    result = await db.execute(
        PlaybackRecord.__table__.update()
        .where(PlaybackRecord.ended_at.is_(None))
        .values(ended_at=PlaybackRecord.last_seen_at)
    )
    await db.commit()
    return result.rowcount or 0


# --------------------------------------------------------------------------
# 客户端限制
# --------------------------------------------------------------------------


def client_violates_policy(user: ManagedUser, client_name: str, device_name: str) -> bool:
    """大小写不敏感的通配符匹配，client 和 device 任一命中即算匹配。"""
    patterns = user.client_patterns
    if user.client_policy_mode == "off" or not patterns:
        return False

    haystack = [client_name.lower(), device_name.lower()]
    matched = any(
        fnmatch.fnmatch(value, pattern.lower())
        for value in haystack
        for pattern in patterns
    )
    if user.client_policy_mode == "allow":
        return not matched
    if user.client_policy_mode == "block":
        return matched
    return False


async def _enforce_client_policy(
    db: AsyncSession,
    client: EmbyClient,
    server: Server,
    entry: LiveSession,
) -> str | None:
    user = await db.scalar(
        select(ManagedUser).where(
            ManagedUser.server_id == server.id,
            ManagedUser.emby_user_id == entry.emby_user_id,
        )
    )
    if user is None or not client_violates_policy(user, entry.client, entry.device_name):
        return None

    label = entry.client or entry.device_name or "未知客户端"
    done: list[str] = []
    failed: list[str] = []

    # 先礼后兵：提示和 Stop 对走 Emby 转发的客户端有效，失败也继续往下走。
    try:
        await client.send_message(
            entry.session_id,
            "客户端不被允许",
            f"当前客户端（{label}）未被管理员授权，播放即将停止。",
        )
    except EmbyError as exc:
        logger.debug("向 %s 发送提示失败: %s", user.username, exc)

    try:
        await client.stop_playback(entry.session_id)
        done.append("已发送停止指令")
    except EmbyError as exc:
        failed.append(f"停止指令失败({exc})")

    # 关键一步：302 直链下媒体流绕过 Emby，只有吊销令牌才能真正切断。
    if entry.device_id:
        try:
            await client.revoke_device(entry.device_id)
            done.append("已吊销设备令牌")
        except EmbyError as exc:
            failed.append(f"吊销令牌失败({exc})")
    else:
        failed.append("会话未上报 DeviceId，无法吊销令牌")

    if not done:
        detail = "；".join(failed) or "未知原因"
        logger.warning("阻断 %s 的 %s 全部失败: %s", user.username, label, detail)
        await log_action(
            db,
            "client_block_failed",
            f"客户端 {label} 违反 {user.client_policy_mode} 规则，但阻断失败：{detail}",
            level="error",
            server_name=server.name,
            username=user.username,
            commit=False,
        )
        await _safe_notify(
            "客户端阻断失败",
            f"{server.name} / {user.username} 使用 {label} 播放，阻断未生效：{detail}",
            category="general",
        )
        return f"阻断失败：{detail}"

    summary = "，".join(done)
    if failed:
        summary += f"（{'；'.join(failed)}）"
    await log_action(
        db,
        "client_blocked",
        f"客户端 {label} 违反 {user.client_policy_mode} 规则，{summary}",
        level="warning",
        server_name=server.name,
        username=user.username,
        commit=False,
    )
    await _safe_notify(
        "客户端被阻断",
        f"{server.name} / {user.username} 使用 {label} 播放，{summary}。",
        category="general",
    )
    return f"已阻断：{label}（{summary}）"


# --------------------------------------------------------------------------
# 启用 / 停用
# --------------------------------------------------------------------------


async def set_user_state(
    db: AsyncSession,
    user: ManagedUser,
    *,
    disabled: bool,
    by_controller: bool,
    reason: str,
    stop_sessions: bool = True,
) -> None:
    """改 Emby 侧 Policy，成功后才更新本地状态。"""
    server = await db.get(Server, user.server_id)
    if server is None:
        raise ValueError("服务器记录不存在")

    async with client_for(server) as client:
        await client.set_user_disabled(user.emby_user_id, disabled)
        if disabled and stop_sessions:
            await _stop_user_sessions(client, user.emby_user_id)

    user.is_disabled = disabled
    user.disabled_by_controller = disabled and by_controller
    if not disabled:
        user.expiry_notified_at = None
        # Manual administrator re-enable is the only recovery path for a
        # bill-code account. Clear the entire ledger so a partial failure
        # history cannot immediately re-lock the account after recovery.
        user.activation_failed_attempts = 0
        user.activation_locked = False
        user.activation_locked_at = None

    await log_action(
        db,
        "user_disabled" if disabled else "user_enabled",
        reason,
        level="warning" if disabled else "info",
        server_name=server.name,
        username=user.username,
        commit=False,
    )
    await db.commit()


async def apply_self_password_policy(db: AsyncSession, allowed: bool) -> dict[str, int]:
    """把「允许在 Emby 客户端改密码」开关下发给所有在管用户。

    设置页翻转开关后必须调用，否则新值只影响之后新注册的账户，
    已有用户仍停留在旧策略上。按服务器分组，一个连接处理该服务器下的全部用户。
    """
    users = list(await db.scalars(select(ManagedUser).order_by(ManagedUser.server_id)))
    result = {"ok": 0, "failed": 0}
    if not users:
        return result

    by_server: dict[int, list[ManagedUser]] = {}
    for user in users:
        by_server.setdefault(user.server_id, []).append(user)

    for server_id, group in by_server.items():
        server = await db.get(Server, server_id)
        if server is None:
            result["failed"] += len(group)
            continue
        try:
            async with client_for(server) as client:
                for user in group:
                    try:
                        await client.set_self_password_allowed(user.emby_user_id, allowed)
                        result["ok"] += 1
                    except EmbyError as exc:
                        result["failed"] += 1
                        logger.warning(
                            "下发改密策略失败 server=%s user=%s: %s",
                            server.name,
                            user.username,
                            exc,
                        )
        except EmbyError as exc:
            # 整台服务器不可达，本轮跳过；下次保存设置或用户续期时会再次写入。
            result["failed"] += len(group)
            logger.warning("服务器 %s 不可达，改密策略未下发：%s", server.name, exc)

    return result


async def _stop_user_sessions(client: EmbyClient, emby_user_id: str) -> None:
    """禁用只挡新登录，已在播的客户端要单独踢掉。"""
    try:
        sessions = await client.list_sessions()
    except EmbyError as exc:
        logger.warning("获取会话列表失败，跳过停止播放: %s", exc)
        return

    for session in sessions:
        if session.get("UserId") != emby_user_id or not session.get("NowPlayingItem"):
            continue
        session_id = session.get("Id")
        if not session_id:
            continue
        try:
            await client.stop_playback(session_id)
        except EmbyError as exc:
            logger.warning("停止会话 %s 失败: %s", session_id, exc)


# --------------------------------------------------------------------------
# 到期处理
# --------------------------------------------------------------------------


async def process_expirations(db: AsyncSession) -> dict[str, int]:
    """到期即禁用；到期前 N 天提醒一次。"""
    now = utcnow()
    stats = {"disabled": 0, "reminded": 0}

    candidates = (
        await db.scalars(
            select(ManagedUser).where(
                ManagedUser.expires_at.is_not(None),
                ManagedUser.is_admin.is_(False),
            )
        )
    ).all()

    for user in candidates:
        expires_at = as_utc(user.expires_at)
        if expires_at is None:
            continue

        if expires_at <= now:
            # 能登用户端的账户到期只收回播放权限，账号留着让用户自己兑换续期。
            # 直接 IsDisabled 会让他连兑换页都用不了，也就无法自救。
            # 注意：进到这里的前提是 expires_at 非空。认领的老账号若管理员没设到期时间，
            # 压根不会被选中，也就永远不会被 purge_expired_users 删掉（即永久保留）；
            # 一旦设了到期时间，它和自助注册账号走同一条回收→删号链路。
            if user.can_portal_login:
                # 回收成功后持久化该时间戳；即使 Emby 被外部重新打开播放，
                # 后续轮询仍会重新关掉权限，但不重复发送同一事件。
                first_revocation = user.playback_revoked_at is None
                if not first_revocation and not user.playback_enabled:
                    continue
                server = await db.get(Server, user.server_id)
                if server is None or not server.enabled:
                    continue
                if user.playback_enabled:
                    try:
                        async with client_for(server) as client:
                            await client.set_playback_allowed(user.emby_user_id, False)
                    except EmbyError as exc:
                        logger.warning("收回 %s 播放权限失败: %s", user.username, exc)
                        await log_action(
                            db,
                            "expire_failed",
                            f"收回播放权限失败: {exc}",
                            level="error",
                            server_name=server.name,
                            username=user.username,
                        )
                        continue
                user.playback_enabled = False
                if not first_revocation:
                    await db.commit()
                    continue
                user.playback_revoked_at = now
                await log_action(
                    db,
                    "playback_revoked",
                    f"到期收回播放权限（到期时间 {_local(expires_at)}），"
                    f"{delete_after_expiry_days()} 天内未续期将删除账户",
                    server_name=server.name,
                    username=user.username,
                    commit=False,
                )
                await db.commit()
                stats["disabled"] += 1
                await _safe_notify(
                    "用户已到期",
                    f"{server.name} / {user.username} 已到期，播放权限已收回。",
                    category="expiry",
                )
                continue

            first_disable = not user.disabled_by_controller
            if user.is_disabled:
                continue
            server = await db.get(Server, user.server_id)
            if server is None or not server.enabled:
                continue
            try:
                await set_user_state(
                    db,
                    user,
                    disabled=True,
                    by_controller=True,
                    reason=f"到期自动停用（到期时间 {_local(expires_at)}）",
                )
            except (EmbyError, ValueError) as exc:
                logger.warning("停用 %s 失败: %s", user.username, exc)
                await log_action(
                    db,
                    "expire_failed",
                    f"自动停用失败: {exc}",
                    level="error",
                    server_name=server.name if server else None,
                    username=user.username,
                )
                continue
            if not first_disable:
                continue
            stats["disabled"] += 1
            await _safe_notify(
                "用户已到期停用",
                f"{server.name} / {user.username} 已到期，账号已停用。",
                category="expiry",
            )
            continue

        remind_days = settings_store.current().expiry_remind_days
        if remind_days <= 0 or user.is_disabled:
            continue
        remind_from = expires_at - timedelta(days=remind_days)
        if now < remind_from or user.expiry_notified_at is not None:
            continue

        remaining_days = max(0, (expires_at - now).days)
        server = await db.get(Server, user.server_id)
        user.expiry_notified_at = now
        await log_action(
            db,
            "expiry_reminder",
            f"距到期还有约 {remaining_days} 天（{_local(expires_at)}）",
            server_name=server.name if server else None,
            username=user.username,
            commit=False,
        )
        await db.commit()
        await _safe_notify(
            "用户即将到期",
            f"{server.name if server else '?'} / {user.username} 将于 "
            f"{_local(expires_at)} 到期，剩余约 {remaining_days} 天。",
            category="expiry",
        )
        stats["reminded"] += 1

    return stats


UNIT_SECONDS = {"minute": 60, "hour": 3600, "day": 86400, "year": 31_536_000}
UNIT_LABELS = {"minute": "分钟", "hour": "小时", "day": "天", "year": "年"}


def activation_grace_hours() -> int:
    """注册后多久不激活就删号。管理员可在后台随时改。"""
    return settings_store.current().activation_grace_hours


def delete_after_expiry_days() -> int:
    """到期关播放后再留多久才真正删号。"""
    return settings_store.current().purge_after_expiry_days


class RegistrationError(RuntimeError):
    """注册/兑换过程中可以直接展示给用户的错误。"""


def duration_to_seconds(amount: int, unit: str) -> int:
    if unit not in UNIT_SECONDS:
        raise RegistrationError("时长单位不合法")
    if amount <= 0:
        raise RegistrationError("时长必须大于 0")
    return amount * UNIT_SECONDS[unit]


def describe_duration(amount: int, unit: str) -> str:
    return f"{amount} {UNIT_LABELS.get(unit, unit)}"


async def count_unused_codes(db: AsyncSession) -> int:
    return await db.scalar(
        select(func.count()).select_from(RedeemCode).where(RedeemCode.used_at.is_(None))
    ) or 0


async def get_register_target(db: AsyncSession) -> Server | None:
    return await db.scalar(
        select(Server).where(
            Server.is_register_target.is_(True), Server.enabled.is_(True)
        )
    )


async def set_register_target(db: AsyncSession, server_id: int | None) -> None:
    """同一时刻只允许一台服务器接收注册。"""
    for server in (await db.scalars(select(Server))).all():
        server.is_register_target = server.id == server_id
    await db.commit()


async def generate_codes(
    db: AsyncSession,
    *,
    amount: int,
    unit: str,
    quantity: int = 1,
    note: str = "",
) -> list[RedeemCode]:
    seconds = duration_to_seconds(amount, unit)
    if quantity < 1 or quantity > 200:
        raise RegistrationError("单次生成数量需在 1-200 之间")

    codes: list[RedeemCode] = []
    for _ in range(quantity):
        code = RedeemCode(
            code=_new_code(),
            duration_seconds=seconds,
            amount=amount,
            unit=unit,
            note=note.strip(),
        )
        db.add(code)
        codes.append(code)

    await log_action(
        db,
        "codes_generated",
        f"生成 {quantity} 个兑换码，每个 {describe_duration(amount, unit)}",
        commit=False,
    )
    await db.commit()
    return codes


def _new_code() -> str:
    """去掉易混字符，用户要手抄。"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    body = "".join(secrets.choice(alphabet) for _ in range(16))
    return "-".join(body[i : i + 4] for i in range(0, 16, 4))


async def register_user(
    db: AsyncSession, *, username: str, password: str
) -> ManagedUser:
    """在注册目标服务器上建号，权限压到最小，播放关闭等兑换码激活。"""
    username = username.strip()
    if not 3 <= len(username) <= 32:
        raise RegistrationError("用户名长度需在 3-32 个字符之间")
    if not username.replace("_", "").replace("-", "").isalnum():
        raise RegistrationError("用户名只能包含字母、数字、下划线和连字符")
    min_length = settings_store.current().portal_password_min_length
    if len(password) < min_length:
        raise RegistrationError(f"密码至少 {min_length} 位")

    server = await get_register_target(db)
    if server is None:
        raise RegistrationError("管理员尚未开放自助注册")

    existing = await db.scalar(
        select(ManagedUser).where(
            ManagedUser.server_id == server.id, ManagedUser.username == username
        )
    )
    if existing is not None:
        # 已开通用户端登录的账户（自助注册或已认领）不能再注册一次。
        if existing.portal_password_hash:
            raise RegistrationError(
                "该用户名已被占用。若这是你本人的账号，请直接用户端登录；"
                "忘记密码请联系管理员重置"
            )
        # 只被同步进来、还没开通用户端登录的 Emby 老账号 —— 走认领。
        return await _claim_existing_user(
            db,
            server=server,
            existing=existing,
            username=existing.username,
            password=password,
        )

    # Emby 侧可能存在控制器还没同步到的同名账户。直接建号会拿到 400/500，
    # 这里先查一遍给出中文提示，并顺手把它同步进本地表，方便管理员认领。
    try:
        async with client_for(server) as probe:
            remote_users = await probe.list_users()
    except EmbyError as exc:
        raise RegistrationError(f"无法连接 Emby 服务器：{exc}") from exc

    remote_match = next(
        (
            entry
            for entry in remote_users
            if (entry.get("Name") or "").lower() == username.lower()
        ),
        None,
    )
    if remote_match is not None:
        # 老账号认领：用 Emby 原密码证明所有权，验证通过就把它纳管并开通用户端登录。
        return await _claim_existing_user(
            db,
            server=server,
            remote_user=remote_match,
            username=username,
            password=password,
        )

    try:
        async with client_for(server) as client:
            created = await client.create_user(username)
            emby_user_id = str(created.get("Id") or "")
            if not emby_user_id:
                raise RegistrationError("Emby 未返回用户 ID，注册失败")
            await client.set_password(emby_user_id, password)
            await client.apply_restricted_policy(
                emby_user_id,
                allow_playback=False,
                allow_self_password=settings_store.current().allow_emby_self_password,
            )
    except EmbyError as exc:
        raise RegistrationError(f"在 Emby 上创建账户失败：{exc}") from exc

    now = utcnow()
    user = ManagedUser(
        server_id=server.id,
        emby_user_id=emby_user_id,
        username=username,
        is_admin=False,
        is_disabled=False,
        portal_password_hash=hash_password(password),
        self_registered=True,
        registered_at=now,
        playback_enabled=False,
        playback_source="controller",
        policy_json=_dump_policy({**RESTRICTED_POLICY, **playback_policy(False)}),
        synced_at=now,
    )
    db.add(user)
    await log_action(
        db,
        "self_register",
        f"自助注册成功，等待兑换码激活（{activation_grace_hours()} 小时内未激活将删除）",
        server_name=server.name,
        username=username,
        commit=False,
    )
    await db.commit()
    await _safe_notify(
        "新用户注册",
        f"{server.name} / {username} 完成自助注册，尚未激活。",
        category="registration",
    )
    return user


def _validate_new_user(username: str, password: str) -> tuple[str, int]:
    username = username.strip()
    if not 3 <= len(username) <= 32:
        raise RegistrationError("用户名长度需在 3-32 个字符之间")
    if not username.replace("_", "").replace("-", "").isalnum():
        raise RegistrationError("用户名只能包含字母、数字、下划线和连字符")
    min_length = settings_store.current().portal_password_min_length
    if len(password) < min_length:
        raise RegistrationError(f"密码至少 {min_length} 位")
    return username, min_length


async def create_managed_user(
    db: AsyncSession,
    *,
    server: Server,
    username: str,
    password: str,
    ordinary_registration: bool,
    allow_playback: bool,
    expires_at: datetime | None,
) -> ManagedUser:
    """管理员创建 Emby 账号，并按普通注册/直接开通写入本地生命周期。"""
    username, _ = _validate_new_user(username, password)
    if not server.enabled:
        raise RegistrationError("所属服务器已暂停")
    existing = await db.scalar(
        select(ManagedUser).where(
            ManagedUser.server_id == server.id,
            ManagedUser.username == username,
        )
    )
    if existing is not None:
        raise RegistrationError("该服务器上已有同名用户")

    emby_user_id = ""
    try:
        async with client_for(server) as client:
            remote_users = await client.list_users()
            if any((entry.get("Name") or "").lower() == username.lower() for entry in remote_users):
                raise RegistrationError("Emby 上已有同名用户")
            created = await client.create_user(username)
            emby_user_id = str(created.get("Id") or "")
            if not emby_user_id:
                raise RegistrationError("Emby 未返回用户 ID，创建失败")
            await client.set_password(emby_user_id, password)
            if ordinary_registration:
                await client.apply_restricted_policy(
                    emby_user_id,
                    allow_playback=False,
                    allow_self_password=settings_store.current().allow_emby_self_password,
                )
            else:
                await client.set_playback_allowed(emby_user_id, allow_playback)
            remote = await client.get_user(emby_user_id)
    except RegistrationError:
        if emby_user_id:
            try:
                async with client_for(server) as cleanup:
                    await cleanup.delete_user(emby_user_id)
            except EmbyError:
                logger.exception("清理管理员创建的半成品 Emby 用户失败：%s", username)
        raise
    except EmbyError as exc:
        if emby_user_id:
            try:
                async with client_for(server) as cleanup:
                    await cleanup.delete_user(emby_user_id)
            except EmbyError:
                logger.exception("清理管理员创建的半成品 Emby 用户失败：%s", username)
        raise RegistrationError(f"创建 Emby 用户失败：{exc}") from exc

    now = utcnow()
    policy = dict(remote.get("Policy") or {})
    user = ManagedUser(
        server_id=server.id,
        emby_user_id=emby_user_id,
        username=username,
        is_disabled=bool(policy.get("IsDisabled")),
        is_admin=bool(policy.get("IsAdministrator")),
        portal_password_hash=hash_password(password),
        self_registered=ordinary_registration,
        portal_enabled=True,
        is_protected=False,
        registered_at=now,
        activated_at=None if ordinary_registration else now,
        expires_at=None if ordinary_registration else expires_at,
        playback_enabled=bool(policy.get("EnableMediaPlayback")) if not ordinary_registration else False,
        playback_source="controller",
        policy_json=_dump_policy(policy),
        synced_at=now,
    )
    db.add(user)
    await log_action(
        db,
        "admin_user_created",
        "管理员创建普通注册账号，等待兑换激活" if ordinary_registration
        else f"管理员直接创建账号（播放{'已开通' if user.playback_enabled else '未开通'}）",
        server_name=server.name,
        username=username,
        commit=False,
    )
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        try:
            async with client_for(server) as cleanup:
                await cleanup.delete_user(emby_user_id)
        except EmbyError:
            logger.exception("数据库失败后清理 Emby 用户失败：%s", username)
        raise RegistrationError("本地保存失败，已尝试回滚 Emby 用户")
    await _safe_notify(
        "账户已创建",
        f"{server.name} / {username}：管理员创建账户。",
        category="registration",
    )
    return user


async def refresh_user_from_emby(db: AsyncSession, user: ManagedUser) -> ManagedUser:
    """读取指定用户最新 Policy；controller 来源只校正播放开关。"""
    server = await db.get(Server, user.server_id)
    if server is None or not server.enabled:
        raise RegistrationError("所属服务器当前不可用")
    try:
        async with client_for(server) as client:
            remote = await client.get_user(user.emby_user_id)
            policy = dict(remote.get("Policy") or {})
            if not policy:
                raise EmbyError("Emby 未返回用户 Policy")
            if user.playback_source == "controller" and not user.is_admin:
                desired = bool(user.playback_enabled)
                playback_drifted = bool(policy.get("EnableMediaPlayback")) != desired
                if not desired:
                    playback_drifted = any(
                        bool(policy.get(field)) for field in PLAYBACK_POLICY_FIELDS
                    )
                if playback_drifted:
                    await client.set_playback_allowed(user.emby_user_id, desired)
                    policy.update(playback_policy(desired))
            else:
                user.playback_enabled = bool(policy.get("EnableMediaPlayback"))
    except EmbyError as exc:
        raise RegistrationError(f"读取 Emby 用户状态失败：{exc}") from exc
    user.is_admin = bool(policy.get("IsAdministrator"))
    if user.activation_locked and not user.is_admin:
        if not bool(policy.get("IsDisabled")):
            try:
                async with client_for(server) as client:
                    await client.set_user_disabled(user.emby_user_id, True)
                policy["IsDisabled"] = True
            except EmbyError as exc:
                logger.warning(
                    "保持锁定账户停用状态失败 user=%s error_type=%s",
                    user.username,
                    type(exc).__name__,
                )
        user.is_disabled = True
    else:
        user.is_disabled = bool(policy.get("IsDisabled"))
    user.policy_json = _dump_policy(policy)
    user.synced_at = utcnow()
    await db.commit()
    return user


async def update_user_policy(
    db: AsyncSession,
    user: ManagedUser,
    changes: dict[str, Any],
) -> ManagedUser:
    server = await db.get(Server, user.server_id)
    if server is None:
        raise RegistrationError("服务器记录不存在")
    try:
        async with client_for(server) as client:
            if set(changes) == {"EnableMediaPlayback"}:
                await client.set_playback_allowed(
                    user.emby_user_id, bool(changes["EnableMediaPlayback"])
                )
            else:
                await client.set_policy_fields(user.emby_user_id, changes)
            remote = await client.get_user(user.emby_user_id)
    except EmbyError as exc:
        raise RegistrationError(f"保存 Emby 权限失败：{exc}") from exc
    policy = dict(remote.get("Policy") or {})
    if not policy:
        policy = policy_for_user(user)
        policy.update(changes)
    user.policy_json = _dump_policy(policy)
    user.is_disabled = bool(policy.get("IsDisabled"))
    user.is_admin = bool(policy.get("IsAdministrator"))
    user.playback_enabled = bool(policy.get("EnableMediaPlayback"))
    if user.playback_source == "controller" and "EnableMediaPlayback" in changes:
        user.playback_enabled = bool(changes["EnableMediaPlayback"])
    user.synced_at = utcnow()
    await log_action(
        db,
        "user_policy_updated",
        f"更新 Emby Policy 字段：{', '.join(changes)}",
        server_name=server.name,
        username=user.username,
        commit=False,
    )
    await db.commit()
    return user


async def _claim_existing_user(
    db: AsyncSession,
    *,
    server: Server,
    username: str,
    password: str,
    remote_user: dict[str, Any] | None = None,
    existing: ManagedUser | None = None,
) -> ManagedUser:
    """认领 Emby 上已存在的账号：验原密码，通过后开通用户端登录。

    认领不修改 Emby 侧密码或播放策略；停用账号不能认领，播放状态始终以
    Emby 返回的 Policy 为准。这样管理员不设到期时间就是永久，设了到期时间
    才会走 process_expirations → purge_expired_users。
    """
    if not settings_store.current().claim_enabled:
        raise RegistrationError(
            "该用户名在 Emby 上已存在。若是你自己的账号，请联系管理员为它开通用户端登录；"
            "否则请改用其他用户名"
        )

    emby_user_id = (
        existing.emby_user_id
        if existing is not None
        else str((remote_user or {}).get("Id") or "")
    )
    if not emby_user_id:
        raise RegistrationError("无法确定该账号在 Emby 上的 ID，请联系管理员")

    # 认领前读取最新 Policy，避免本地同步缓存落后导致停用账号被认领，
    # 也确保最终展示的播放权限来自 Emby 实际状态。
    policy: dict[str, Any] = (remote_user or {}).get("Policy") or {}
    if not policy:
        try:
            async with client_for(server) as client:
                policy = (await client.get_user(emby_user_id)).get("Policy") or {}
        except EmbyError as exc:
            raise RegistrationError(f"无法读取 Emby 账号状态：{exc}") from exc
    if not policy:
        raise RegistrationError("无法读取 Emby 账号状态，请联系管理员")
    if bool(policy.get("IsDisabled")):
        raise RegistrationError("该账户已停用，请联系管理员")

    is_admin_remote = bool(policy.get("IsAdministrator")) or (
        existing is not None and existing.is_admin
    )
    if is_admin_remote:
        # 管理账号不允许通过自助流程认领 —— 拿到用户端入口等于绕过管理端鉴权。
        raise RegistrationError("该账号为管理账号，请联系管理员处理")

    if _claim_rate_limited(username):
        await log_action(
            db,
            "claim_rate_limited",
            f"认领尝试过于频繁，已拒绝（{CLAIM_MAX_ATTEMPTS} 次 /"
            f" {int(CLAIM_ATTEMPT_WINDOW.total_seconds() // 60)} 分钟）",
            level="warning",
            server_name=server.name,
            username=username,
        )
        raise RegistrationError("验证失败次数过多，请稍后再试或联系管理员")

    try:
        async with client_for(server) as client:
            verified = await client.verify_credentials(username, password)
    except EmbyError as exc:
        raise RegistrationError(f"无法连接 Emby 服务器：{exc}") from exc

    if not verified:
        raise RegistrationError(
            "该用户名在 Emby 上已存在。若是你本人的账号，请填写它在 Emby 上的现有密码完成认领；"
            "否则请改用其他用户名"
        )

    now = utcnow()
    if existing is None:
        # 先把这台服务器同步一遍，拿到带 Policy 的完整记录，避免字段靠猜。
        try:
            await sync_server_users(db, server)
        except (EmbyError, ValueError) as exc:
            logger.warning("认领前同步 %s 失败: %s", server.name, exc)
        existing = await db.scalar(
            select(ManagedUser).where(
                ManagedUser.server_id == server.id,
                ManagedUser.emby_user_id == emby_user_id,
            )
        )
    if existing is None:
        raise RegistrationError("同步该账号失败，请联系管理员")
    if existing.is_admin:
        raise RegistrationError("该账号为管理账号，请联系管理员处理")

    _claim_attempts_reset(username)
    existing.portal_password_hash = hash_password(password)
    existing.portal_enabled = True
    existing.claimed_at = now
    existing.claim_requested_at = None
    existing.playback_enabled = bool(policy.get("EnableMediaPlayback"))
    existing.playback_source = "emby"
    existing.is_disabled = bool(policy.get("IsDisabled"))
    existing.policy_json = _dump_policy(policy)
    existing.playback_revoked_at = None
    # 认领即视为已激活：这是 Emby 上在用的真实账号，不是等兑换码激活的新注册。
    # 不设 expires_at —— 按约定「管理员没设到期时间就是永久」。
    if existing.activated_at is None:
        existing.activated_at = now
    existing.synced_at = now
    # self_registered 保持 False：这不是新号，未激活删号不该碰它。
    await log_action(
        db,
        "claim_existing",
        "认领 Emby 已有账号，已开通用户端登录并标记为已激活"
        f"（播放{'已开通' if existing.playback_enabled else '未开通'}，"
        "沿用 Emby 现状；未设到期时间即永久）",
        server_name=server.name,
        username=existing.username,
        commit=False,
    )
    await db.commit()
    await _safe_notify(
        "老账号认领",
        f"{server.name} / {existing.username} 通过 Emby 原密码认领，已开通用户端登录。",
        category="registration",
    )
    return existing


async def authenticate_portal_user(
    db: AsyncSession, username: str, password: str
) -> ManagedUser | None:
    """自助注册账户与管理员开通过用户端的老账户都能登录。

    用户名在多台服务器上可能重复，逐个候选验密码，命中即返回。
    """
    candidates = (
        await db.scalars(
            select(ManagedUser).where(ManagedUser.username == username.strip())
        )
    ).all()
    for user in candidates:
        if not user.can_portal_login:
            continue
        if verify_password(password, user.portal_password_hash):
            return user
    return None


async def enable_portal_login(
    db: AsyncSession, user: ManagedUser, password: str
) -> None:
    """给 Emby 上已存在的账户开通用户端登录（「认领」老账号）。

    与自助注册的关键差别：**不设 `self_registered`**，因此这个账户
    永远不会被「未激活删号」和「到期删号」两个任务碰到 —— 老账户是管理员
    自己或朋友在用的，不该因为没兑换过码就被自动清掉。

    密码同步写去 Emby，保持「以 portal 为准，单向下发」的既有口径。
    权限策略一概不动：老账户的媒体库范围是管理员配好的，收敛到
    RESTRICTED_POLICY 会把人家的现有权限抹掉。
    """
    if user.is_admin:
        raise RegistrationError("管理员账号不要开通用户端登录，避免管理凭据外流")
    min_length = settings_store.current().portal_password_min_length
    if len(password) < min_length:
        raise RegistrationError(f"密码至少 {min_length} 位")

    server = await db.get(Server, user.server_id)
    if server is None or not server.enabled:
        raise RegistrationError("所属服务器当前不可用，请稍后再试")
    try:
        async with client_for(server) as client:
            await client.set_password(user.emby_user_id, password)
            remote = await client.get_user(user.emby_user_id)
    except EmbyError as exc:
        raise RegistrationError(f"同步密码到 Emby 失败：{exc}") from exc

    user.portal_password_hash = hash_password(password)
    user.portal_enabled = True
    user.playback_source = "emby"
    if remote.get("Policy"):
        user.policy_json = _dump_policy(dict(remote["Policy"]))
        user.playback_enabled = bool(remote["Policy"].get("EnableMediaPlayback"))
        user.is_disabled = bool(remote["Policy"].get("IsDisabled"))
    if user.activated_at is None:
        user.activated_at = utcnow()
    # 老账户不参与自动删号，同时标记受保护，双重防误删。
    user.is_protected = True
    await log_action(
        db,
        "portal_login_enabled",
        "管理员为已有 Emby 账户开通用户端登录（不参与自动删号）",
        server_name=server.name,
        username=user.username,
        commit=False,
    )
    await db.commit()
    await _safe_notify(
        "老账号认领",
        f"{server.name} / {user.username} 已由管理员开通用户端登录。",
        category="registration",
    )


async def disable_portal_login(db: AsyncSession, user: ManagedUser) -> None:
    """撤回用户端登录。清掉密码哈希，下一次请求就会被踢回登录页。"""
    user.portal_enabled = False
    user.portal_password_hash = ""
    await log_action(
        db,
        "portal_login_disabled",
        "管理员撤回用户端登录权限",
        username=user.username,
        commit=False,
    )
    await db.commit()


async def set_user_protected(
    db: AsyncSession, user: ManagedUser, protected: bool
) -> None:
    """切换防误删标记。管理员账号强制受保护，不允许解除。"""
    if user.is_admin and not protected:
        raise RegistrationError("管理员账号必须保持受保护状态")
    user.is_protected = protected
    await log_action(
        db,
        "user_protection_changed",
        "已标记为受保护（禁止删除）" if protected else "已解除受保护标记",
        level="warning" if not protected else "info",
        username=user.username,
        commit=False,
    )
    await db.commit()


async def admin_delete_user(db: AsyncSession, user: ManagedUser) -> str:
    async with _user_mutation_lock:
        return await _admin_delete_user(db, user)


async def _admin_delete_user(db: AsyncSession, user: ManagedUser) -> str:
    """管理员手动删号：先删 Emby 账户，成功后再删本地记录。

    Emby 删除失败就整体放弃并把错误抛给界面 —— 绝不能只删本地影子记录，
    那会留下一个控制器管不到、却仍能登录 Emby 的账户。
    """
    if user.is_admin:
        raise RegistrationError("管理员账号不允许删除")
    if user.is_protected:
        raise RegistrationError("该账号已标记为受保护，请先解除保护再删除")

    server = await db.get(Server, user.server_id)
    if server is None:
        raise RegistrationError("服务器记录不存在")
    if not server.enabled:
        raise RegistrationError("所属服务器已暂停监控，恢复后再删除以确保 Emby 侧同步删除")

    try:
        async with client_for(server) as client:
            await client.delete_user(user.emby_user_id)
    except EmbyError as exc:
        await log_action(
            db,
            "user_delete_failed",
            f"管理员删除失败，未做任何改动: {exc}",
            level="error",
            server_name=server.name,
            username=user.username,
        )
        raise RegistrationError(f"删除 Emby 账户失败，已中止：{exc}") from exc

    username = user.username
    await _delete_local_user_data(db, user, server)
    await _safe_notify(
        "账户已删除", f"{server.name} / {username}：管理员手动删除。", category="general"
    )
    return username


async def redeem_code(db: AsyncSession, user: ManagedUser, raw_code: str) -> RedeemCode:
    """兑换成功则累加时长；首次兑换视为激活并开放播放。

    到期时间从「当前到期时间」或「现在」中较晚者起算，
    这样提前续期不会白送也不会吞掉剩余时间。
    """
    if user.activation_locked:
        raise RegistrationError("激活码功能已停用，账户已停用")
    code_text = raw_code.strip().upper()
    if not code_text:
        raise RegistrationError("请输入兑换码")

    code = await db.scalar(select(RedeemCode).where(RedeemCode.code == code_text))
    if code is None:
        await _record_activation_failure(db, user)
        raise RegistrationError("兑换码不存在")
    if code.is_used:
        await _record_activation_failure(db, user)
        raise RegistrationError("该兑换码已被使用")
    now = utcnow()
    if code.expires_at is not None and as_utc(code.expires_at) <= now:
        await _record_activation_failure(db, user)
        raise RegistrationError("该兑换码已失效")
    if code.owner_user_id is not None and code.owner_user_id != user.id:
        await _record_activation_failure(db, user)
        raise RegistrationError("该兑换码不属于当前账户")

    base = as_utc(user.expires_at)
    if base is None or base < now:
        base = now
    new_expires = base + timedelta(seconds=code.duration_seconds)

    server = await db.get(Server, user.server_id)
    if server is None or not server.enabled:
        raise RegistrationError("所属服务器当前不可用，请稍后再试")

    first_activation = not user.is_activated
    try:
        async with client_for(server) as client:
            if user.is_disabled:
                await client.set_user_disabled(user.emby_user_id, False)
            await client.set_playback_allowed(user.emby_user_id, True)
    except EmbyError as exc:
        raise RegistrationError(f"开通播放权限失败：{exc}") from exc

    user.expires_at = new_expires
    user.expiry_notified_at = None
    user.playback_enabled = True
    user.playback_revoked_at = None
    user.is_disabled = False
    user.activation_failed_attempts = 0
    if first_activation:
        user.activated_at = now

    code.used_by_user_id = user.id
    code.used_by_username = user.username
    code.used_at = now
    code.resulting_expires_at = new_expires

    await log_action(
        db,
        "redeem",
        f"兑换 {describe_duration(code.amount, code.unit)}，"
        f"到期时间更新为 {_local(new_expires)}",
        server_name=server.name,
        username=user.username,
        commit=False,
    )
    await db.commit()
    await _safe_notify(
        "兑换码已使用",
        f"{server.name} / {user.username} 兑换 {describe_duration(code.amount, code.unit)}，"
        f"到期 {_local(new_expires)}。",
        category="registration",
    )
    return code


async def change_portal_password(
    db: AsyncSession, user: ManagedUser, old_password: str, new_password: str
) -> None:
    if not verify_password(old_password, user.portal_password_hash):
        raise RegistrationError("当前密码不正确")
    min_length = settings_store.current().portal_password_min_length
    if len(new_password) < min_length:
        raise RegistrationError(f"新密码至少 {min_length} 位")

    server = await db.get(Server, user.server_id)
    if server is None or not server.enabled:
        raise RegistrationError("所属服务器当前不可用，请稍后再试")
    try:
        async with client_for(server) as client:
            await client.set_password(user.emby_user_id, new_password)
    except EmbyError as exc:
        raise RegistrationError(f"修改 Emby 密码失败：{exc}") from exc

    user.portal_password_hash = hash_password(new_password)
    await log_action(
        db,
        "portal_password_changed",
        "用户自行修改密码",
        server_name=server.name,
        username=user.username,
        commit=False,
    )
    await db.commit()


async def change_portal_username(
    db: AsyncSession, user: ManagedUser, new_username: str
) -> None:
    new_username = new_username.strip()
    if new_username == user.username:
        return
    if not 3 <= len(new_username) <= 32:
        raise RegistrationError("用户名长度需在 3-32 个字符之间")
    if not new_username.replace("_", "").replace("-", "").isalnum():
        raise RegistrationError("用户名只能包含字母、数字、下划线和连字符")

    taken = await db.scalar(
        select(ManagedUser).where(
            ManagedUser.server_id == user.server_id,
            ManagedUser.username == new_username,
            ManagedUser.id != user.id,
        )
    )
    if taken is not None:
        raise RegistrationError("该用户名已被占用")

    server = await db.get(Server, user.server_id)
    if server is None or not server.enabled:
        raise RegistrationError("所属服务器当前不可用，请稍后再试")
    try:
        async with client_for(server) as client:
            await client.rename_user(user.emby_user_id, new_username)
    except EmbyError as exc:
        raise RegistrationError(f"修改 Emby 用户名失败：{exc}") from exc

    old_name = user.username
    user.username = new_username
    await log_action(
        db,
        "portal_username_changed",
        f"用户名由 {old_name} 改为 {new_username}",
        server_name=server.name,
        username=new_username,
        commit=False,
    )
    await db.commit()


async def purge_inactive_registrations(db: AsyncSession) -> int:
    """注册满宽限期仍未激活的账户，连同 Emby 账号一起删除。宽限期在后台设置页调整。"""
    grace = activation_grace_hours()
    cutoff = utcnow() - timedelta(hours=grace)
    candidates = (
        await db.scalars(
            select(ManagedUser).where(
                ManagedUser.self_registered.is_(True),
                ManagedUser.activated_at.is_(None),
                ManagedUser.registered_at.is_not(None),
            )
        )
    ).all()

    removed = 0
    for user in candidates:
        registered_at = as_utc(user.registered_at)
        if registered_at is None or registered_at > cutoff:
            continue
        if await _delete_managed_user(
            db, user, reason=f"注册 {grace} 小时内未激活，自动删除"
        ):
            removed += 1
    return removed


async def purge_expired_users(db: AsyncSession) -> int:
    """播放权限被回收满 N 天仍未续期的账户，删除。

    覆盖自助注册账户与被认领的 Emby 老账号。老账号只在管理员给它设过
    expires_at 时才可能进来 —— playback_revoked_at 由 process_expirations 写，
    而那里要求 expires_at 非空。没设到期时间的老账号即永久保留。
    """
    retain_days = delete_after_expiry_days()
    cutoff = utcnow() - timedelta(days=retain_days)
    candidates = (
        await db.scalars(
            select(ManagedUser).where(
                or_(
                    ManagedUser.self_registered.is_(True),
                    ManagedUser.claimed_at.is_not(None),
                ),
                ManagedUser.playback_revoked_at.is_not(None),
            )
        )
    ).all()

    removed = 0
    for user in candidates:
        revoked_at = as_utc(user.playback_revoked_at)
        if revoked_at is None or revoked_at > cutoff:
            continue
        expires_at = as_utc(user.expires_at)
        if expires_at is not None and expires_at > utcnow():
            continue  # 期间已续期
        if await _delete_managed_user(
            db, user, reason=f"到期超过 {retain_days} 天未续期，自动删除"
        ):
            removed += 1
    return removed


async def _delete_managed_user(
    db: AsyncSession, user: ManagedUser, *, reason: str
) -> bool:
    async with _user_mutation_lock:
        return await _delete_managed_user_locked(db, user, reason=reason)


async def _delete_managed_user_locked(
    db: AsyncSession, user: ManagedUser, *, reason: str
) -> bool:
    """先删 Emby 账号再删本地记录。Emby 删除失败就留着下轮重试。"""
    # 兜底：自动任务永远不碰管理员与受保护账号，即使调用方的筛选条件写错了。
    if not user.deletable:
        logger.info("跳过受保护账号 %s 的自动删除", user.username)
        return False
    server = await db.get(Server, user.server_id)
    if server is None or not server.enabled:
        return False
    try:
        async with client_for(server) as client:
            await client.delete_user(user.emby_user_id)
    except EmbyError as exc:
        logger.warning("删除 Emby 用户 %s 失败: %s", user.username, exc)
        await log_action(
            db,
            "user_delete_failed",
            f"自动删除失败: {exc}",
            level="error",
            server_name=server.name,
            username=user.username,
        )
        return False

    username = user.username
    await _delete_local_user_data(db, user, server)
    await _safe_notify(
        "账户已删除", f"{server.name} / {username}：{reason}", category="expiry"
    )
    return True


async def _delete_local_user_data(db: AsyncSession, user: ManagedUser, server: Server) -> None:
    """Delete a confirmed-removed account and its data under the playback lock."""
    from . import scheduler

    identity = (user.server_id, user.emby_user_id)
    async with _playback_lock:
        paths = set((await db.scalars(select(MediaRequest.poster_local_path).where(
            MediaRequest.managed_user_id == user.id,
            MediaRequest.poster_local_path.is_not(None),
        ))).all())
        names = set((await db.scalars(select(PlaybackRecord.username).where(
            PlaybackRecord.server_id == user.server_id,
            PlaybackRecord.emby_user_id == user.emby_user_id,
        ))).all()) | {user.username}
        other_names = set((await db.scalars(select(ManagedUser.username).where(
            ManagedUser.id != user.id, ManagedUser.username.in_(names),
        ))).all())
        await db.execute(delete(ActionLog).where(
            or_(
                (ActionLog.username.in_(names)) & (ActionLog.server_name == server.name),
                (ActionLog.username.in_(names - other_names)) & ActionLog.server_name.is_(None),
            )
        ))
        await db.execute(delete(PlaybackRecord).where(
            PlaybackRecord.server_id == user.server_id,
            PlaybackRecord.emby_user_id == user.emby_user_id,
        ))
        await db.execute(delete(BillCodeUsage).where(BillCodeUsage.user_id == user.id))
        await db.execute(delete(BillCodeDailyUsage).where(BillCodeDailyUsage.user_id == user.id))
        await db.execute(delete(RedeemCode).where(or_(
            RedeemCode.owner_user_id == user.id, RedeemCode.used_by_user_id == user.id,
        )))
        await db.execute(delete(MediaRequest).where(MediaRequest.managed_user_id == user.id))
        await db.delete(user)
        await db.commit()
        # A poll can publish its result after this transaction has completed.
        _deleted_playback_users.add(identity)
        for key, entry in list(_playback_cache.items()):
            if (entry.server_id, entry.emby_user_id) == identity:
                _playback_cache.pop(key, None)
                _playback_generations.pop(key, None)
        scheduler.remove_live_sessions_for_user(*identity)
        for name in names - other_names:
            _claim_attempts.pop(name, None)

    for relative in paths:
        referenced = await db.scalar(select(MediaRequest.id).where(MediaRequest.poster_local_path == relative).limit(1))
        shared = await db.scalar(select(MediaRequestSummary.id).where(MediaRequestSummary.poster_local_path == relative).limit(1))
        if referenced is None and shared is None:
            path = _safe_image_path(relative)
            if path is not None:
                try:
                    path.unlink()
                except OSError:
                    logger.warning("清理已删除用户海报失败")


async def purge_old_history(db: AsyncSession) -> int:
    from .stats import playback_retention_start

    cutoff = playback_retention_start()
    result = await db.execute(
        delete(PlaybackRecord).where(
            PlaybackRecord.ended_at.is_not(None),
            PlaybackRecord.started_at < cutoff,
        )
    )
    await db.commit()
    return result.rowcount or 0


def _summary_from_request(
    row: MediaRequest,
    *,
    status: str | None = None,
    processed_at: datetime | None = None,
    processed_by: str | None = None,
    rejection_reason: str | None = None,
) -> MediaRequestSummary:
    terminal_status = status or row.status
    terminal_time = processed_at or (
        as_utc(row.confirmed_at) if terminal_status == "in_library" else as_utc(row.rejected_at)
    ) or as_utc(row.created_at) or utcnow()
    return MediaRequestSummary(
        server_id=row.server_id,
        tmdb_id=row.tmdb_id,
        media_type=row.media_type,
        title=row.title,
        original_title=row.original_title,
        year=row.year,
        overview=row.overview,
        poster_url=row.poster_url,
        poster_local_path=row.poster_local_path,
        status=terminal_status,
        processed_at=terminal_time,
        processed_by=processed_by or (
            row.confirmed_by if terminal_status == "in_library" else row.rejected_by
        ),
        rejection_reason=rejection_reason if rejection_reason is not None else row.rejection_reason,
        detail_snapshot=getattr(row, "detail_snapshot", "{}") or "{}",
    )


async def _upsert_request_summary(
    db: AsyncSession,
    row: MediaRequest,
    *,
    status: str,
    processed_at: datetime,
    processed_by: str | None,
    rejection_reason: str = "",
) -> MediaRequestSummary:
    summary = await db.scalar(
        select(MediaRequestSummary).where(
            MediaRequestSummary.server_id == row.server_id,
            MediaRequestSummary.tmdb_id == row.tmdb_id,
            MediaRequestSummary.media_type == row.media_type,
        )
    )
    if summary is None:
        summary = _summary_from_request(
            row,
            status=status,
            processed_at=processed_at,
            processed_by=processed_by,
            rejection_reason=rejection_reason,
        )
        db.add(summary)
    else:
        summary.title = row.title
        summary.original_title = row.original_title
        summary.year = row.year
        summary.overview = row.overview
        summary.poster_url = row.poster_url
        summary.poster_local_path = row.poster_local_path or summary.poster_local_path
        summary.status = status
        summary.processed_at = processed_at
        summary.processed_by = processed_by
        summary.rejection_reason = rejection_reason
        summary.detail_snapshot = getattr(row, "detail_snapshot", "{}") or "{}"
    return summary


def _safe_image_path(relative_path: str | None) -> Path | None:
    if not relative_path:
        return None
    root = get_settings().image_dir.resolve()
    path = (root / relative_path).resolve()
    if root not in path.parents or not path.is_file():
        return None
    return path


async def purge_old_tmdb_history(db: AsyncSession) -> int:
    """清理过期终态求片明细和海报，保留服务器级状态摘要。"""
    retention = settings_store.current().tmdb_retention_days
    if retention <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=retention)
    rows = (
        await db.scalars(
            select(MediaRequest).where(MediaRequest.status.in_(("in_library", "rejected")))
        )
    ).all()
    expired: list[MediaRequest] = []
    for row in rows:
        terminal_at = row.confirmed_at if row.status == "in_library" else row.rejected_at
        terminal_at = as_utc(terminal_at) or as_utc(row.created_at)
        if terminal_at is not None and terminal_at <= cutoff:
            expired.append(row)
    if not expired:
        return 0

    paths = {row.poster_local_path for row in expired if row.poster_local_path}
    for row in expired:
        await _upsert_request_summary(
            db,
            row,
            status=row.status,
            processed_at=(as_utc(row.confirmed_at) if row.status == "in_library" else as_utc(row.rejected_at)) or utcnow(),
            processed_by=row.confirmed_by if row.status == "in_library" else row.rejected_by,
            rejection_reason=row.rejection_reason,
        )
        await db.delete(row)

    # 摘要只保留远程 URL，海报文件按保留期回收；仍被未过期记录引用的文件不删。
    for summary in (
        await db.scalars(select(MediaRequestSummary).where(MediaRequestSummary.poster_local_path.is_not(None)))
    ).all():
        if summary.poster_local_path in paths:
            summary.poster_local_path = None
    remaining_paths = {
        path
        for path in (
            await db.scalars(select(MediaRequest.poster_local_path).where(MediaRequest.poster_local_path.is_not(None)))
        ).all()
        if path
    }
    await db.commit()
    for relative in paths - remaining_paths:
        path = _safe_image_path(relative)
        if path is None:
            continue
        try:
            path.unlink()
        except OSError:
            logger.warning("清理 TMDB 海报失败：%s", relative)
    return len(expired)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite 取回的 datetime 可能没有 tzinfo，统一补成 UTC。"""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _local(value: datetime) -> str:
    from zoneinfo import ZoneInfo

    return value.astimezone(ZoneInfo(_settings.timezone)).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------
# 求片
# --------------------------------------------------------------------------

MEDIA_REQUEST_STATUSES = {"pending", "in_library", "rejected"}
MEDIA_TYPES = {"movie", "tv"}

def moviepilot_enabled() -> bool:
    r = settings_store.current()
    return bool(str(getattr(r, "moviepilot_url", "") or "").strip())

def _mp_type(value: str) -> str:
    return "电影" if value == "movie" else "电视剧"

def _mp_people(value: Any, *, with_character: bool = False) -> list[dict[str, Any]]:
    """Normalize MoviePilot actor/crew variants into small JSON objects."""
    if isinstance(value, dict):
        for key in ("cast", "actors", "crew", "directors", "results", "items"):
            nested = value.get(key)
            if isinstance(nested, list):
                value = nested
                break
        else:
            value = list(value.values())
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for entry in value:
        if isinstance(entry, str):
            name = entry.strip()
            person = {"name": name} if name else None
        elif isinstance(entry, dict):
            name = str(
                entry.get("name")
                or entry.get("full_name")
                or entry.get("original_name")
                or ""
            ).strip()
            if not name:
                continue
            person = {
                "id": entry.get("id") or entry.get("person_id"),
                "name": name,
                "profile_url": entry.get("profile_url") or entry.get("profile") or entry.get("profile_path"),
            }
            if with_character:
                person["character"] = entry.get("character") or entry.get("role")
            person = {key: value for key, value in person.items() if value not in (None, "")}
            profile = person.get("profile_url")
            if isinstance(profile, str) and profile.startswith("/"):
                person["profile_url"] = "https://image.tmdb.org/t/p/w185" + profile
        else:
            continue
        if person:
            rows.append(person)
    return rows

def _mp_seasons(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = [
            ({**entry, "season_number": entry.get("season_number", key)} if isinstance(entry, dict) else {
                "season_number": key,
                "episode_count": len(entry) if isinstance(entry, (list, tuple)) else entry,
            })
            for key, entry in value.items()
        ]
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        number = entry.get("season_number")
        if number is None:
            number = entry.get("number")
        if number is None:
            number = entry.get("season")
        try:
            number = int(number)
        except (TypeError, ValueError):
            continue
        count = entry.get("episode_count") or entry.get("episodes") or entry.get("number_of_episodes") or 0
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 0
        poster = entry.get("poster_url") or entry.get("poster_path") or entry.get("poster")
        if isinstance(poster, str) and poster.startswith("/"):
            poster = "https://image.tmdb.org/t/p/w342" + poster
        rows.append({
            "season_number": number,
            "name": str(entry.get("name") or entry.get("title") or f"第 {number} 季"),
            "episode_count": count,
            "overview": str(entry.get("overview") or "") or None,
            "air_date": str(entry.get("air_date") or entry.get("release_date") or "") or None,
            "poster_url": poster or None,
        })
    return rows

def _normalize_mp(item: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict): return None
    # Some MoviePilot builds wrap media details in ``media_info`` or ``media``.
    # Flatten that payload while retaining top-level fields such as existence
    # and subscription metadata.
    nested = item.get("media_info") or item.get("media") or item.get("result")
    if isinstance(nested, dict):
        item = {**item, **nested}
    source = str(item.get("media_source") or item.get("source") or "themoviedb").strip()
    if source == "tmdb": source = "themoviedb"
    # MoviePilot's ``MediaInfo`` payloads are not fully uniform across V3
    # releases.  Details often expose ``tmdb_id`` without ``media_id`` and
    # some search responses use ``title_year``/``original_name``.  Always
    # derive a stable media id and human title before falling back to the id.
    mid = str(
        item.get("media_id")
        or item.get("id")
        or item.get("tmdb_id")
        or item.get("tvdb_id")
        or item.get("imdb_id")
        or ""
    ).strip()
    title = str(
        item.get("title")
        or item.get("name")
        or item.get("original_title")
        or item.get("original_name")
        or item.get("media_name")
        or item.get("display_name")
        or item.get("title_year")
        or ""
    ).strip()
    if not mid or not title: return None
    typ = str(item.get("type") or item.get("type_name") or item.get("media_type") or "").lower()
    media_type = "tv" if typ in {"tv", "电视剧", "series"} else "movie"
    year = item.get("year") or item.get("release_date") or item.get("first_air_date")
    try: year = int(str(year)[:4]) if year else None
    except (TypeError, ValueError): year = None
    poster = (
        item.get("poster_url")
        or item.get("poster_path")
        or item.get("poster")
        or item.get("image")
        or item.get("cover")
        or item.get("cover_url")
        or ""
    )
    if isinstance(poster, str) and poster.startswith("/"):
        base = str(settings_store.current().moviepilot_url or "").rstrip("/")
        poster = base + poster
    raw_seasons = item.get("number_of_seasons")
    if raw_seasons is None or isinstance(raw_seasons, (list, dict)):
        raw_seasons = item.get("season_count")
    raw_episodes = item.get("number_of_episodes")
    if raw_episodes is None or isinstance(raw_episodes, (list, dict)):
        raw_episodes = item.get("total_episode")
    if raw_episodes is None or isinstance(raw_episodes, (list, dict)):
        raw_episodes = item.get("episode_count")
    seasons = raw_seasons
    episodes = raw_episodes
    try: seasons = int(seasons) if seasons is not None else None
    except (TypeError, ValueError): seasons = None
    try: episodes = int(episodes) if episodes is not None else None
    except (TypeError, ValueError): episodes = None
    backdrop = str(item.get("backdrop_url") or item.get("backdrop_path") or item.get("backdrop") or "").strip()
    if backdrop.startswith("/"):
        backdrop = str(settings_store.current().moviepilot_url or "").rstrip("/") + backdrop
    season_info = _mp_seasons(item.get("season_info") or item.get("seasons_info") or item.get("seasons_detail") or item.get("seasons") or [])
    if seasons is None and season_info:
        seasons = len(season_info)
    if episodes is None and season_info:
        episodes = sum(int(row.get("episode_count") or 0) for row in season_info)
    episodes_info = item.get("episodes_info") or item.get("episode_info")
    if not isinstance(episodes_info, (dict, list)):
        raw_episodes = item.get("episodes")
        episodes_info = raw_episodes if isinstance(raw_episodes, (dict, list)) else {}
    # MoviePilot's detail payload exposes the episode numbers in ``seasons``
    # (a mapping of season number to a list of episode numbers), while the
    # portal snapshot uses a stable episodes_info mapping.  Preserve those
    # numbers so expanding a season never needs another MoviePilot request.
    raw_season_episodes = item.get("seasons")
    if isinstance(raw_season_episodes, dict):
        generated: dict[str, list[dict[str, Any]]] = {}
        for key, values in raw_season_episodes.items():
            if isinstance(values, list):
                generated[str(key)] = [
                    {"episode_number": int(v)} if str(v).lstrip("-").isdigit() else {"episode_number": i + 1}
                    for i, v in enumerate(values)
                ]
        if generated:
            if isinstance(episodes_info, dict):
                episodes_info = {**generated, **episodes_info}
            elif not episodes_info:
                episodes_info = generated
    raw_genres = item.get("genres") or item.get("genre") or []
    genres = []
    if isinstance(raw_genres, list):
        genres = [str(g.get("name") or g.get("title") or "").strip() if isinstance(g, dict) else str(g).strip() for g in raw_genres]
        genres = [g for g in genres if g]
    raw_crew = item.get("directors") or item.get("crew") or []
    if isinstance(raw_crew, dict) and isinstance(raw_crew.get("crew"), list):
        raw_crew = raw_crew["crew"]
    directors = _mp_people(raw_crew)
    if isinstance(raw_crew, list) and any(isinstance(entry, dict) and entry.get("job") for entry in raw_crew):
        director_rows = [entry for entry in raw_crew if isinstance(entry, dict) and str(entry.get("job") or "").casefold() == "director"]
        # MoviePilot labels the primary creator of some documentary series as
        # Producer. Keep that credit visible when no explicit director exists.
        if not director_rows:
            director_rows = [entry for entry in raw_crew if isinstance(entry, dict) and str(entry.get("job") or "").casefold() in {"producer", "executive producer"}]
        directors = _mp_people(director_rows)
    producers = _mp_people([entry for entry in raw_crew if isinstance(entry, dict) and str(entry.get("job") or "").casefold() in {"producer", "executive producer"}]) if isinstance(raw_crew, list) else []
    cast = _mp_people(item.get("cast") or item.get("actors") or item.get("credits") or [], with_character=True)
    return {
        "media_source": source,
        "media_id": mid,
        "tmdb_id": int(mid) if source in {"tmdb", "themoviedb"} and mid.isdigit() else 0,
        "media_type": media_type,
        "title": title,
        "original_title": str(item.get("original_title") or item.get("original_name") or title),
        "imdb_id": item.get("imdb_id") or item.get("imdbid") or item.get("imdb"),
        "tvdb_id": item.get("tvdb_id") or item.get("tvdbid") or item.get("tvdb"),
        "year": year,
        "overview": str(item.get("overview") or ""),
        "poster_url": str(poster),
        "backdrop_url": backdrop or None,
        "release_date": item.get("release_date") or item.get("first_air_date"),
        "tagline": item.get("tagline"),
        "rating": item.get("vote_average") or item.get("rating"),
        "runtime_minutes": item.get("runtime_minutes") or item.get("runtime"),
        "status": item.get("status"),
        "seasons": seasons,
        "episodes": episodes,
        "genres": genres,
        "season_info": season_info,
        "episodes_info": episodes_info,
        "episode_groups": item.get("episode_groups") if isinstance(item.get("episode_groups"), list) else [],
        "directors": directors,
        "producers": producers,
        "cast": cast,
    }

async def search_moviepilot(query: str, media_type: str | None = None) -> list[dict[str, Any]]:
    try:
        async with MoviePilotClient() as client:
            rows = await client.search_media(query, media_type=_mp_type(media_type) if media_type in MEDIA_TYPES else None)
    except MoviePilotError as exc: raise RegistrationError(str(exc)) from exc
    normalized = [x for x in (_normalize_mp(r) for r in rows) if x]
    normalized = [x for x in normalized if media_type not in MEDIA_TYPES or x["media_type"] == media_type]
    async def check(item: dict[str, Any]) -> None:
        key = (item["media_source"], item["media_id"], item["media_type"])
        cached = _moviepilot_exists_cache.get(key)
        if cached and time.monotonic() - cached[0] < 60:
            item["library_state"] = "in_library" if cached[1] is True else "not_in_library" if cached[1] is False else "unknown"
            return
        try:
            async with _moviepilot_exists_sem:
                async with MoviePilotClient() as c:
                    value = await c.exists(mtype=_mp_type(item["media_type"]), media_source=item["media_source"], media_id=item["media_id"], title=item["title"], year=item.get("year"))
            _moviepilot_exists_cache[key] = (time.monotonic(), value)
            item["library_state"] = "in_library" if value is True else "not_in_library" if value is False else "unknown"
        except MoviePilotError:
            item["library_state"] = "unknown"
    await asyncio.gather(*(check(item) for item in normalized))
    return normalized

async def moviepilot_details(media_source: str, media_id: str, media_type: str | None = None) -> dict[str, Any]:
    try:
        async with MoviePilotClient() as client:
            row = await client.detail(
                media_source,
                media_id,
                _mp_type(media_type) if media_type in MEDIA_TYPES else None,
            )
            result = _normalize_mp(row)
            # ``media/{id}`` normally includes the default TMDB season list,
            # but some MoviePilot builds omit it and expose only episode
            # groups.  Resolve the first group here so a submitted request
            # can persist season metadata in its database snapshot.
            if (
                result
                and result.get("media_type") == "tv"
                and result.get("tmdb_id")
                and not result.get("season_info")
            ):
                try:
                    groups = await client.episode_groups(result["tmdb_id"])
                except MoviePilotError:
                    groups = []
                if groups:
                    result["episode_groups"] = groups
                    group_id = str(groups[0].get("id") or "").strip()
                    if group_id:
                        try:
                            grouped_seasons = await client.group_seasons(group_id)
                        except MoviePilotError:
                            grouped_seasons = []
                        if grouped_seasons:
                            result["season_info"] = _mp_seasons(grouped_seasons)
    except MoviePilotError as exc: raise RegistrationError(str(exc)) from exc
    result = result or {"media_source": media_source, "media_id": media_id, "tmdb_id": int(media_id) if str(media_id).isdigit() else 0, "media_type": media_type or "movie", "title": media_id}
    # MoviePilot installations can return a compact identity-only payload.
    # Enrich that response once from TMDB when a numeric TMDB identity is
    # available, then keep the merged result in the caller's snapshot.
    normalized_type = result.get("media_type") or media_type or "movie"
    numeric_id = int(result.get("tmdb_id") or 0)
    placeholder_title = not str(result.get("title") or "").strip() or str(result.get("title") or "").strip().isdigit()
    needs_tmdb = numeric_id > 0 and normalized_type in MEDIA_TYPES and (
        placeholder_title
        or not result.get("poster_url")
        or not result.get("overview")
        or (normalized_type == "tv" and not result.get("season_info"))
    )
    if needs_tmdb:
        try:
            async with TmdbClient() as client:
                tmdb = await client.details(normalized_type, numeric_id)
        except Exception:
            tmdb = {}
        if tmdb:
            merged = dict(tmdb)
            for key, value in result.items():
                if value in (None, "", [], {}):
                    continue
                if key == "title" and placeholder_title:
                    continue
                merged[key] = value
            result = merged
            result["media_source"] = media_source
            result["media_id"] = str(media_id)
            result["tmdb_id"] = numeric_id
    try:
        async with MoviePilotClient() as client:
            value = await client.exists(mtype=_mp_type(result["media_type"]), media_source=media_source, media_id=media_id, title=result.get("title", ""), year=result.get("year"))
        result["library_state"] = "in_library" if value is True else "not_in_library" if value is False else "unknown"
    except MoviePilotError:
        result["library_state"] = "unknown"
    return result
TMDB_MODES = {"multi", "movie", "tv", "movie_id", "tv_id"}


def _validate_tmdb_query(mode: str, query: str) -> tuple[str, str]:
    mode = mode.strip().lower()
    query = query.strip()
    if mode not in TMDB_MODES:
        raise RegistrationError("无效的搜索类型")
    if mode.endswith("_id"):
        if not query.isdigit() or int(query) <= 0:
            raise RegistrationError("TMDB ID 必须是正整数")
    elif not 1 <= len(query) <= 100:
        raise RegistrationError("搜索关键词长度需在 1-100 个字符之间")
    return mode, query


async def search_tmdb(mode: str, query: str, year: str = "") -> list[dict[str, Any]]:
    if moviepilot_enabled():
        mode = mode.strip().lower()
        mt = "movie" if mode == "movie" else "tv" if mode == "tv" else None
        if mode.endswith("_id"):
            mt = "movie" if mode == "movie_id" else "tv"
            return [await moviepilot_details("themoviedb", query, mt)]
        if not query.strip():
            raise RegistrationError("请输入搜索关键词")
        return await search_moviepilot(query.strip(), mt)
    mode, query = _validate_tmdb_query(mode, query)
    try:
        year_value = int(year.strip()) if year.strip() else None
    except ValueError as exc:
        raise RegistrationError("年份必须是数字") from exc
    if year_value is not None and not 1900 <= year_value <= 2200:
        raise RegistrationError("年份范围不正确")
    try:
        async with TmdbClient() as client:
            if mode.endswith("_id"):
                media_type = "movie" if mode == "movie_id" else "tv"
                result = await client.details(media_type, int(query))
                _tmdb_details_cache[(media_type, int(query))] = (time.monotonic(), dict(result))
                return [result]
            results = await client.search(mode, query, year_value)
            # 搜索结果是轻量列表，不能充当完整详情缓存；详情页始终走
            # ``details(..., append_to_response=credits)`` 并单独缓存。
            return results
    except TmdbError as exc:
        raise RegistrationError(str(exc)) from exc


async def tmdb_details(media_type: str, tmdb_id: int) -> dict[str, Any]:
    if moviepilot_enabled():
        return await moviepilot_details("themoviedb", str(tmdb_id), media_type)
    if media_type not in MEDIA_TYPES:
        raise RegistrationError("媒体类型无效")
    cache_key = (media_type, tmdb_id)
    cached = _tmdb_details_cache.get(cache_key)
    # Ignore legacy/lightweight entries that may still exist in a running
    # process after an upgrade; only full detail payloads are reusable here.
    is_full_detail = cached and "directors" in cached[1] and "cast" in cached[1]
    if is_full_detail and time.monotonic() - cached[0] < _TMDB_DETAILS_TTL:
        return dict(cached[1])
    try:
        async with TmdbClient() as client:
            result = await client.details(media_type, tmdb_id)
    except TmdbError as exc:
        raise RegistrationError(str(exc)) from exc
    _tmdb_details_cache[cache_key] = (time.monotonic(), dict(result))
    return result


async def create_media_request(
    db: AsyncSession,
    user: ManagedUser,
    *,
    tmdb_id: int,
    media_type: str,
    note: str = "",
    media_source: str = "tmdb",
    media_id: str = "",
    seasons: list[int] | None = None,
    detail_override: dict[str, Any] | None = None,
) -> MediaRequest:
    # MoviePilot V3 uses ``themoviedb`` as the provider identifier.  Accept
    # legacy Apex ``tmdb`` values from older clients while persisting one
    # stable identity for duplicate detection and status refreshes.
    if moviepilot_enabled() and str(media_source or "").strip().lower() in {"", "tmdb"}:
        media_source = "themoviedb"
    if media_type not in MEDIA_TYPES or (tmdb_id <= 0 and not moviepilot_enabled()):
        raise RegistrationError("作品信息无效")
    note = note.strip()
    if len(note) > 1000:
        raise RegistrationError("备注不能超过 1000 个字符")

    identity_source, identity_id = media_source, (media_id or str(tmdb_id))
    if moviepilot_enabled() and identity_id:
        identity_rows = (await db.scalars(select(MediaRequest).where(MediaRequest.server_id == user.server_id, MediaRequest.managed_user_id == user.id, MediaRequest.media_source == identity_source, MediaRequest.media_id == identity_id))).all()
        if any(r.status == "in_library" for r in identity_rows): raise RegistrationError("该作品已入库")
        if any(r.status == "pending" for r in identity_rows): raise RegistrationError("你已经求过这部作品，请勿重复提交")

    already_library = await db.scalar(
        select(MediaRequest.id).where(
            MediaRequest.server_id == user.server_id,
            MediaRequest.tmdb_id == tmdb_id,
            MediaRequest.media_type == media_type,
            MediaRequest.status == "in_library",
        ).limit(1)
    )
    summary_library = await db.scalar(
        select(MediaRequestSummary.id).where(
            MediaRequestSummary.server_id == user.server_id,
            MediaRequestSummary.tmdb_id == tmdb_id,
            MediaRequestSummary.media_type == media_type,
            MediaRequestSummary.status == "in_library",
        ).limit(1)
    )
    if already_library is not None or summary_library is not None:
        raise RegistrationError("该作品已入库")

    existing = (
        await db.scalars(
            select(MediaRequest).where(
                MediaRequest.server_id == user.server_id,
                MediaRequest.managed_user_id == user.id,
                MediaRequest.tmdb_id == tmdb_id,
                MediaRequest.media_type == media_type,
            )
        )
    ).all()
    if any(item.status == "in_library" for item in existing):
        raise RegistrationError("该作品已入库")
    if any(item.status == "pending" for item in existing):
        raise RegistrationError("你已经求过这部作品，请勿重复提交")

    detail = await tmdb_details(media_type, tmdb_id) if not moviepilot_enabled() else await moviepilot_details(media_source, media_id or str(tmdb_id), media_type)
    supplied = _normalize_mp(detail_override or {}) if isinstance(detail_override, dict) else None
    if supplied:
        # Search results already contain useful metadata.  Use it to repair a
        # compact/empty MoviePilot detail response without another upstream
        # lookup, while keeping richer fields from the detail response.
        for key, value in supplied.items():
            current = detail.get(key)
            invalid = current in (None, "", [], {}) or (key == "title" and str(current).strip().isdigit())
            if invalid and value not in (None, "", [], {}):
                detail[key] = value
    server = await db.get(Server, user.server_id)
    request_row = MediaRequest(
        server_id=user.server_id,
        managed_user_id=user.id,
        tmdb_id=int(detail.get("tmdb_id") or (tmdb_id if tmdb_id > 0 else 0)),
        media_type=detail["media_type"],
        title=detail.get("title") or detail.get("original_title") or f"TMDB {tmdb_id}",
        original_title=detail.get("original_title") or detail.get("title") or "",
        year=detail.get("year"),
        overview=detail.get("overview") or "",
        poster_url=detail.get("poster_url") or "",
        note=note,
        status="pending",
        media_source=media_source,
        media_id=media_id or str(tmdb_id),
        season_numbers=json.dumps(seasons or [], ensure_ascii=False),
        detail_snapshot=json.dumps(
            {key: detail.get(key) for key in (
                "tmdb_id", "media_source", "media_id", "media_type", "title",
                "original_title", "year", "overview", "poster_url", "backdrop_url",
                "release_date", "tagline", "status", "rating", "runtime_minutes",
                "genres", "directors", "producers", "cast", "seasons", "episodes",
                "season_info", "episodes_info",
            ) if detail.get(key) is not None}, ensure_ascii=False
        ),
        library_state="unknown",
    )
    if moviepilot_enabled():
        try:
            async with MoviePilotClient() as client:
                chosen = seasons if media_type == "tv" and seasons else [None]
                ids: list[int] = []
                for season in chosen:
                    sid = await client.subscribe(name=request_row.title, media_type=_mp_type(media_type), media_source=media_source, media_id=media_id or str(tmdb_id), year=request_row.year, season=season)
                    ids.append(sid)
                    await client.pause(sid)
                request_row.moviepilot_subscribe_ids = json.dumps(ids)
                request_row.moviepilot_subscribe_state = "S"
        except MoviePilotError as exc:
            request_row.moviepilot_error = str(exc)
            await db.rollback()
            raise RegistrationError(str(exc)) from exc
    db.add(request_row)
    await log_action(
        db,
        "media_requested",
        f"提交{detail['title']}（TMDB {tmdb_id}）",
        server_name=server.name if server else None,
        username=user.username,
        commit=False,
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # The partial unique index closes the race between concurrent submits.
        raise RegistrationError("你已经求过这部作品，请勿重复提交") from exc
    await _safe_notify(
        "新求片",
        f"{server.name if server else '?'} / {user.username} 求片：{request_row.title}（TMDB {request_row.tmdb_id}）。",
        category="media_request",
    )
    return request_row


async def portal_media_requests(
    db: AsyncSession, user: ManagedUser
) -> dict[str, list[MediaRequest]]:
    own = (
        await db.scalars(
            select(MediaRequest)
            .where(MediaRequest.managed_user_id == user.id)
            .order_by(MediaRequest.created_at.desc())
        )
    ).all()
    library = (
        await db.scalars(
            select(MediaRequest)
            .where(
                MediaRequest.server_id == user.server_id,
                MediaRequest.status == "in_library",
            )
            .order_by(MediaRequest.confirmed_at.desc(), MediaRequest.created_at.desc())
        )
    ).all()
    library_unique: list[MediaRequest] = []
    seen_library: set[tuple[int, str]] = set()
    for item in library:
        key = (item.tmdb_id, item.media_type)
        if key in seen_library:
            continue
        seen_library.add(key)
        library_unique.append(item)
    summary_rows = (
        await db.scalars(
            select(MediaRequestSummary).where(
                MediaRequestSummary.server_id == user.server_id,
                MediaRequestSummary.status == "in_library",
            ).order_by(MediaRequestSummary.processed_at.desc())
        )
    ).all()
    existing_keys = {(item.tmdb_id, item.media_type) for item in library_unique}
    for summary in summary_rows:
        key = (summary.tmdb_id, summary.media_type)
        if key in existing_keys:
            continue
        # 复用 MediaRequest 形状供管理端 API 序列化使用。
        library_unique.append(
            MediaRequest(
                id=0,
                server_id=summary.server_id,
                managed_user_id=user.id,
                tmdb_id=summary.tmdb_id,
                media_type=summary.media_type,
                title=summary.title,
                original_title=summary.original_title,
                year=summary.year,
                overview=summary.overview,
                poster_url=summary.poster_url,
                poster_local_path=None,
                note="",
                status="in_library",
                confirmed_at=summary.processed_at,
                confirmed_by=summary.processed_by,
                rejection_reason="",
                poster_error="",
                media_source="themoviedb",
                media_id=str(summary.tmdb_id),
                detail_snapshot=summary.detail_snapshot or "{}",
            )
        )
        existing_keys.add(key)
    return {
        "pending": [item for item in own if item.status == "pending"],
        "library": library_unique,
        "rejected": [item for item in own if item.status == "rejected"],
    }


async def admin_media_request_groups(
    db: AsyncSession, status: str = "pending"
) -> list[dict[str, Any]]:
    if status not in MEDIA_REQUEST_STATUSES and status != "all":
        status = "pending"
    stmt = (
        select(MediaRequest, ManagedUser.username, Server.name)
        .join(ManagedUser, ManagedUser.id == MediaRequest.managed_user_id)
        .join(Server, Server.id == MediaRequest.server_id)
        .order_by(MediaRequest.created_at.desc())
    )
    if status != "all":
        stmt = stmt.where(MediaRequest.status == status)
    rows = (await db.execute(stmt)).all()
    groups: dict[tuple[int, int, str], dict[str, Any]] = {}
    for request_row, username, server_name in rows:
        key = (request_row.server_id, request_row.tmdb_id, request_row.media_type)
        group = groups.setdefault(
            key,
            {
                "server_id": request_row.server_id,
                "server_name": server_name,
                "tmdb_id": request_row.tmdb_id,
                "media_type": request_row.media_type,
                "title": request_row.title,
                "original_title": request_row.original_title,
                "year": request_row.year,
                "overview": request_row.overview,
                "poster_url": request_row.poster_url,
                "poster_local_path": request_row.poster_local_path,
                "poster_error": request_row.poster_error,
                "status": request_row.status,
                "items": [],
            },
        )
        group["items"].append({"request": request_row, "username": username})
        if not group["poster_local_path"] and request_row.poster_local_path:
            group["poster_local_path"] = request_row.poster_local_path
        if not group["poster_error"] and request_row.poster_error:
            group["poster_error"] = request_row.poster_error
        if request_row.status == "pending":
            group["status"] = "pending"
    return list(groups.values())


def _poster_path(server_id: int, media_type: str, tmdb_id: int) -> Path:
    return get_settings().image_dir / "posters" / f"{server_id}-{media_type}-{tmdb_id}.jpg"


async def _cache_confirmed_poster(
    *,
    server_id: int,
    media_type: str,
    tmdb_id: int,
    poster_url: str,
) -> None:
    """后台缓存已确认作品的海报，不影响确认响应。"""
    from .db import SessionLocal

    poster_path = _poster_path(server_id, media_type, tmdb_id)
    relative_path: str | None = None
    poster_error = ""
    try:
        # 海报单独使用短超时；即使上游不可达，确认状态也已经持久化。
        timeout = min(5.0, max(1.0, float(get_settings().http_timeout_seconds)))
        async with TmdbClient(timeout=timeout) as client:
            content, _content_type = await client.download_poster(poster_url)
        poster_path.parent.mkdir(parents=True, exist_ok=True)
        poster_path.write_bytes(content)
        # 数据库存 POSIX 相对路径，跨 Windows 开发环境和 Linux 容器一致。
        relative_path = poster_path.relative_to(get_settings().image_dir).as_posix()
    except (TmdbError, OSError, ValueError) as exc:
        poster_error = str(exc)
        logger.warning(
            "确认入库后的 TMDB 海报缓存失败（server=%s type=%s id=%s）：%s",
            server_id,
            media_type,
            tmdb_id,
            poster_error,
        )

    try:
        async with SessionLocal() as task_db:
            rows = (
                await task_db.scalars(
                    select(MediaRequest).where(
                        MediaRequest.server_id == server_id,
                        MediaRequest.media_type == media_type,
                        MediaRequest.tmdb_id == tmdb_id,
                        MediaRequest.status == "in_library",
                    )
                )
            ).all()
            for row in rows:
                row.poster_local_path = relative_path
                row.poster_error = poster_error
            summaries = (
                await task_db.scalars(
                    select(MediaRequestSummary).where(
                        MediaRequestSummary.server_id == server_id,
                        MediaRequestSummary.media_type == media_type,
                        MediaRequestSummary.tmdb_id == tmdb_id,
                    )
                )
            ).all()
            for summary in summaries:
                summary.poster_local_path = relative_path
            await task_db.commit()
    except Exception:
        # 状态已在主请求中提交，后台海报更新失败不能反向影响它。
        logger.exception(
            "保存确认入库海报状态失败（server=%s type=%s id=%s）",
            server_id,
            media_type,
            tmdb_id,
        )


async def _push_confirm_notification(message: str) -> None:
    await _safe_notify("求片已入库", message, category="media_request")


async def confirm_media_request_group(
    db: AsyncSession,
    *,
    server_id: int,
    media_type: str,
    tmdb_id: int,
    confirmed_by: str,
) -> int:
    if media_type not in MEDIA_TYPES or tmdb_id <= 0:
        raise RegistrationError("作品信息无效")
    pending_rows = (
        await db.scalars(
            select(MediaRequest).where(
                MediaRequest.server_id == server_id,
                MediaRequest.media_type == media_type,
                MediaRequest.tmdb_id == tmdb_id,
                MediaRequest.status == "pending",
            )
        )
    ).all()
    if not pending_rows:
        raise RegistrationError("没有待处理的求片记录")
    # 有待处理项时仍按既定组语义统一更新该服务器该作品的全部明细，
    # 包括此前被拒绝的历史，避免一个作品出现互相矛盾的组状态。
    rows = (
        await db.scalars(
            select(MediaRequest).where(
                MediaRequest.server_id == server_id,
                MediaRequest.media_type == media_type,
                MediaRequest.tmdb_id == tmdb_id,
            )
        )
    ).all()

    first = next((row for row in rows if row.poster_url), rows[0])

    now = utcnow()
    server = await db.get(Server, server_id)
    for row in rows:
        row.status = "in_library"
        row.confirmed_at = now
        row.confirmed_by = confirmed_by
        row.poster_local_path = None
        row.poster_error = ""
        row.rejection_reason = ""
        await _upsert_request_summary(
            db,
            row,
            status="in_library",
            processed_at=now,
            processed_by=confirmed_by,
            rejection_reason="",
        )
    await log_action(
        db,
        "media_request_confirmed",
        f"确认入库 TMDB {tmdb_id}，共 {len(rows)} 条请求",
        server_name=server.name if server else None,
        commit=False,
    )
    await db.commit()
    title = rows[0].title if rows else f"TMDB {tmdb_id}"
    if first.poster_url:
        task_key = (server_id, media_type, tmdb_id)
        if task_key not in _poster_tasks:
            _poster_tasks.add(task_key)
            task = asyncio.create_task(
                _cache_confirmed_poster(
                    server_id=server_id,
                    media_type=media_type,
                    tmdb_id=tmdb_id,
                    poster_url=first.poster_url,
                ),
                name=f"tmdb-poster-{server_id}-{media_type}-{tmdb_id}",
            )

            def _release_poster_task(_task: asyncio.Task[None]) -> None:
                _poster_tasks.discard(task_key)
                _background_tasks.discard(_task)

            _background_tasks.add(task)
            task.add_done_callback(_release_poster_task)
    notification_task = asyncio.create_task(
        _push_confirm_notification(
            f"{server.name if server else '?'}：{title} 已确认入库，共更新 {len(rows)} 条请求。"
        ),
        name=f"media-request-notify-{server_id}-{media_type}-{tmdb_id}",
    )
    _background_tasks.add(notification_task)
    notification_task.add_done_callback(_background_tasks.discard)
    return len(rows)


async def reject_media_request_group(
    db: AsyncSession,
    *,
    server_id: int,
    media_type: str,
    tmdb_id: int,
    rejected_by: str,
    reason: str = "",
) -> int:
    reason = reason.strip()
    if len(reason) > 1000:
        raise RegistrationError("拒绝原因不能超过 1000 个字符")
    rows = (
        await db.scalars(
            select(MediaRequest).where(
                MediaRequest.server_id == server_id,
                MediaRequest.media_type == media_type,
                MediaRequest.tmdb_id == tmdb_id,
                MediaRequest.status == "pending",
            )
        )
    ).all()
    if not rows:
        raise RegistrationError("没有待处理的求片记录")
    now = utcnow()
    server = await db.get(Server, server_id)
    for row in rows:
        await _upsert_request_summary(
            db,
            row,
            status="rejected",
            processed_at=now,
            processed_by=rejected_by,
            rejection_reason=reason,
        )
        row.status = "rejected"
        row.rejected_at = now
        row.rejected_by = rejected_by
        row.rejection_reason = reason
    await log_action(
        db,
        "media_request_rejected",
        f"拒绝 TMDB {tmdb_id}，共 {len(rows)} 条请求：{reason or '未填写原因'}",
        server_name=server.name if server else None,
        commit=False,
    )
    await db.commit()
    title = rows[0].title if rows else f"TMDB {tmdb_id}"
    await _safe_notify(
        "求片已拒绝",
        f"{server.name if server else '?'}：{title} 已拒绝，共更新 {len(rows)} 条请求。",
        category="media_request",
    )
    return len(rows)
