"""后台定时任务。"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from . import services, settings_store
from .config import get_settings
from .db import SessionLocal

logger = logging.getLogger(__name__)
_settings = get_settings()

scheduler = AsyncIOScheduler(timezone=_settings.timezone)

# 后台主动轮询维护的当前播放缓存，仪表盘直接读缓存。
live_cache: list[services.LiveSession] = []


def update_live_session(entry: services.LiveSession) -> None:
    """兼容旧调用方，合并一个实时会话到缓存。"""
    global live_cache
    key = (entry.server_id, entry.session_id, entry.item_id)
    live_cache = [
        item
        for item in live_cache
        if (item.server_id, item.session_id, item.item_id) != key
    ]
    live_cache.append(entry)


def remove_live_session(
    server_id: int,
    session_id: str,
    item_id: str | None = None,
) -> None:
    """兼容旧调用方，从仪表盘缓存移除一个会话。"""
    global live_cache
    live_cache = [
        item
        for item in live_cache
        if not (
            item.server_id == server_id
            and item.session_id == session_id
            and (item_id is None or item.item_id == item_id)
        )
    ]


def replace_live_sessions(
    sessions: list[services.LiveSession],
    successful_server_ids: set[int],
    enabled_server_ids: set[int],
) -> None:
    """只替换成功轮询服务器的缓存，保留失败服务器上一轮的结果。"""
    global live_cache
    live_cache = [
        item
        for item in live_cache
        if item.server_id not in successful_server_ids
        and item.server_id in enabled_server_ids
    ]
    live_cache.extend(sessions)


async def _playback_job() -> None:
    try:
        async with SessionLocal() as db:
            result = await services.poll_sessions_by_server(db)
        replace_live_sessions(
            result.live,
            result.successful_server_ids,
            result.enabled_server_ids,
        )
    except Exception:
        logger.exception("正在播放轮询任务异常")


async def _expiry_job() -> None:
    try:
        async with SessionLocal() as db:
            stats = await services.process_expirations(db)
        if stats["disabled"] or stats["reminded"]:
            logger.info(
                "到期检查：停用 %s，提醒 %s", stats["disabled"], stats["reminded"]
            )
    except Exception:
        logger.exception("到期检查任务异常")


async def _sync_job() -> None:
    try:
        async with SessionLocal() as db:
            await services.sync_all_servers(db)
    except Exception:
        logger.exception("用户同步任务异常")


async def _purge_job() -> None:
    try:
        async with SessionLocal() as db:
            removed = await services.purge_old_history(db)
            tmdb_removed = await services.purge_old_tmdb_history(db)
        if removed or tmdb_removed:
            logger.info("清理过期记录：播放历史 %s 条，TMDB 求片 %s 条", removed, tmdb_removed)
    except Exception:
        logger.exception("历史清理任务异常")


async def _lifecycle_job() -> None:
    """删号两类：注册后超时未激活的，以及到期后超过保留期的。

    两个动作都会连带删除 Emby 侧账户，所以放在同一个 job 里串行跑，
    失败只记日志，下一轮会重试。
    """
    try:
        async with SessionLocal() as db:
            inactive = await services.purge_inactive_registrations(db)
        if inactive:
            logger.info("清理未激活注册账户 %s 个", inactive)
    except Exception:
        logger.exception("未激活注册清理任务异常")

    try:
        async with SessionLocal() as db:
            expired = await services.purge_expired_users(db)
        if expired:
            logger.info("清理到期账户 %s 个", expired)
    except Exception:
        logger.exception("到期账户清理任务异常")


def _lifecycle_interval(grace_hours: int) -> int:
    """未激活宽限期按小时算，检查间隔取宽限期的 1/4，最短 5 分钟、最长 1 小时。

    保证超时后最迟一个间隔内就被清掉，又不会空转太频繁。
    """
    return min(60, max(5, grace_hours * 15))


def start() -> None:
    if scheduler.running:
        return
    # Lifespan tests and graceful restarts can start the same scheduler again;
    # avoid duplicate job IDs left by a previous shutdown.
    scheduler.remove_all_jobs()
    cfg = settings_store.current()
    scheduler.add_job(
        _expiry_job,
        IntervalTrigger(minutes=cfg.expiry_check_minutes),
        id="process_expirations",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _playback_job,
        IntervalTrigger(seconds=cfg.poll_interval_seconds),
        id="poll_playback",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _sync_job,
        IntervalTrigger(minutes=15),
        id="sync_users",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _purge_job,
        IntervalTrigger(hours=24),
        id="purge_history",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _lifecycle_job,
        IntervalTrigger(minutes=_lifecycle_interval(cfg.activation_grace_hours)),
        id="account_lifecycle",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    # 启动后立即检查一次到期、生命周期和当前播放。
    scheduler.add_job(_expiry_job, id="expiry_now", replace_existing=True)
    scheduler.add_job(_lifecycle_job, id="lifecycle_now", replace_existing=True)
    scheduler.add_job(_playback_job, id="playback_now", replace_existing=True)


def reschedule() -> None:
    """后台改完间隔类配置后调用，让新间隔立刻生效。

    调度器没起来（比如 portal 进程）时直接跳过。
    """
    if not scheduler.running:
        return
    cfg = settings_store.current()
    intervals = {
        "poll_playback": IntervalTrigger(seconds=cfg.poll_interval_seconds),
        "process_expirations": IntervalTrigger(minutes=cfg.expiry_check_minutes),
        "account_lifecycle": IntervalTrigger(
            minutes=_lifecycle_interval(cfg.activation_grace_hours)
        ),
    }
    for job_id, trigger in intervals.items():
        if scheduler.get_job(job_id) is not None:
            scheduler.reschedule_job(job_id, trigger=trigger)


async def shutdown() -> None:
    if scheduler.running:
        scheduler.pause()
        async with SessionLocal() as db:
            await services.flush_playback_cache(db)
        await services.wait_background_tasks()
        scheduler.shutdown(wait=False)
