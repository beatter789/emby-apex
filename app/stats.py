"""播放历史聚合查询。"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .models import ManagedUser, PlaybackRecord, Server, utcnow

_settings = get_settings()
PLAYBACK_STATS_RETENTION_DAYS = 180


@dataclass
class AggregateRow:
    label: str
    sublabel: str
    plays: int
    hours: float


def _hours(seconds: float | None) -> float:
    return round((seconds or 0) / 3600, 1)


async def top_users(db: AsyncSession, days: int, limit: int = 15) -> list[AggregateRow]:
    since = utcnow() - timedelta(days=days)
    rows = (
        await db.execute(
            select(
                PlaybackRecord.username,
                func.count(PlaybackRecord.id),
                func.sum(PlaybackRecord.watched_seconds),
            )
            .where(PlaybackRecord.started_at >= since)
            .group_by(PlaybackRecord.username)
            .order_by(func.sum(PlaybackRecord.watched_seconds).desc())
            .limit(limit)
        )
    ).all()
    return [
        AggregateRow(label=name or "未知", sublabel="", plays=plays, hours=_hours(seconds))
        for name, plays, seconds in rows
    ]


async def top_items(db: AsyncSession, days: int, limit: int = 15) -> list[AggregateRow]:
    since = utcnow() - timedelta(days=days)
    rows = (
        await db.execute(
            select(
                PlaybackRecord.item_name,
                PlaybackRecord.series_name,
                func.count(PlaybackRecord.id),
                func.sum(PlaybackRecord.watched_seconds),
            )
            .where(PlaybackRecord.started_at >= since)
            .group_by(PlaybackRecord.item_name, PlaybackRecord.series_name)
            .order_by(func.sum(PlaybackRecord.watched_seconds).desc())
            .limit(limit)
        )
    ).all()
    return [
        AggregateRow(
            label=name or "未知",
            sublabel=series or "",
            plays=plays,
            hours=_hours(seconds),
        )
        for name, series, plays, seconds in rows
    ]


async def top_clients(db: AsyncSession, days: int, limit: int = 15) -> list[AggregateRow]:
    since = utcnow() - timedelta(days=days)
    rows = (
        await db.execute(
            select(
                PlaybackRecord.client,
                func.count(PlaybackRecord.id),
                func.sum(PlaybackRecord.watched_seconds),
            )
            .where(PlaybackRecord.started_at >= since)
            .group_by(PlaybackRecord.client)
            .order_by(func.count(PlaybackRecord.id).desc())
            .limit(limit)
        )
    ).all()
    return [
        AggregateRow(
            label=client or "未知", sublabel="", plays=plays, hours=_hours(seconds)
        )
        for client, plays, seconds in rows
    ]


async def recent_records(db: AsyncSession, limit: int = 60) -> list[PlaybackRecord]:
    return list(
        (
            await db.scalars(
                select(PlaybackRecord)
                .order_by(PlaybackRecord.started_at.desc())
                .limit(limit)
            )
        ).all()
    )


async def totals(db: AsyncSession, days: int) -> dict[str, float | int]:
    since = utcnow() - timedelta(days=days)
    plays, seconds, users = (
        await db.execute(
            select(
                func.count(PlaybackRecord.id),
                func.sum(PlaybackRecord.watched_seconds),
                func.count(func.distinct(PlaybackRecord.emby_user_id)),
            ).where(PlaybackRecord.started_at >= since)
        )
    ).one()
    return {"plays": plays or 0, "hours": _hours(seconds), "users": users or 0}


async def today_watch_time(db: AsyncSession) -> dict[str, object]:
    """Return ended playback time for the current day in the app timezone."""
    return await watch_time_for_date(db)


class WatchDateError(ValueError):
    pass


def watch_date_bounds() -> tuple[date, date]:
    today = utcnow().astimezone(ZoneInfo(_settings.timezone)).date()
    return today - timedelta(days=PLAYBACK_STATS_RETENTION_DAYS - 1), today


def playback_retention_start() -> datetime:
    first, _ = watch_date_bounds()
    return datetime.combine(first, time.min, tzinfo=ZoneInfo(_settings.timezone)).astimezone(timezone.utc)


def _watch_date_range(selected_date: date | None = None) -> tuple[date, datetime, datetime]:
    zone = ZoneInfo(_settings.timezone)
    first, today = watch_date_bounds()
    target = selected_date or today
    if target > today:
        raise WatchDateError("不能查询未来日期")
    if target < first:
        raise WatchDateError("观看时长仅保留最近 180 天")
    start = datetime.combine(target, time.min, tzinfo=zone).astimezone(timezone.utc)
    end = datetime.combine(target + timedelta(days=1), time.min, tzinfo=zone).astimezone(
        timezone.utc
    )
    return target, start, end


async def watch_time_for_date(
    db: AsyncSession, selected_date: date | None = None
) -> dict[str, object]:
    """Return ended playback time for one retained calendar day."""
    target, start, end = _watch_date_range(selected_date)
    filters = (
        PlaybackRecord.ended_at.is_not(None),
        PlaybackRecord.ended_at >= start,
        PlaybackRecord.ended_at < end,
    )
    total_seconds = (
        await db.scalar(
            select(func.sum(PlaybackRecord.watched_seconds)).where(*filters)
        )
    ) or 0
    rows = (
        await db.execute(
            select(
                PlaybackRecord.server_id,
                PlaybackRecord.emby_user_id,
                func.coalesce(ManagedUser.username, func.max(PlaybackRecord.username)),
                Server.name,
                func.count(PlaybackRecord.id),
                func.sum(PlaybackRecord.watched_seconds),
            )
            .join(Server, Server.id == PlaybackRecord.server_id)
            .outerjoin(ManagedUser, (ManagedUser.server_id == PlaybackRecord.server_id)
                       & (ManagedUser.emby_user_id == PlaybackRecord.emby_user_id))
            .where(*filters)
            .group_by(PlaybackRecord.server_id, PlaybackRecord.emby_user_id, ManagedUser.username, Server.name)
            .order_by(func.sum(PlaybackRecord.watched_seconds).desc(), PlaybackRecord.server_id, PlaybackRecord.emby_user_id)
        )
    ).all()
    user_rows = [
        {
            "server_id": server_id,
            "emby_user_id": emby_user_id,
            "server_name": server_name,
            "username": name or "未知",
            "plays": int(plays or 0),
            "hours": _hours(seconds),
            "seconds": float(seconds or 0),
        }
        for server_id, emby_user_id, name, server_name, plays, seconds in rows
    ]
    duplicate_names = {
        name for name in (item["username"] for item in user_rows)
        if sum(1 for other in user_rows if other["username"] == name) > 1
    }
    users = []
    for item in user_rows:
        if item["username"] not in duplicate_names:
            item = {key: value for key, value in item.items()
                    if key not in {"server_id", "emby_user_id", "server_name", "seconds"}}
        users.append(item)
    return {
        "date": target.isoformat(),
        "min_date": watch_date_bounds()[0].isoformat(),
        "max_date": watch_date_bounds()[1].isoformat(),
        "hours": _hours(total_seconds),
        "seconds": float(total_seconds),
        "users": users,
    }


async def user_playback_summaries(
    db: AsyncSession,
) -> dict[tuple[int, str], dict[str, object]]:
    """Compatibility view of lifetime playback counters by stable user identity."""
    rows = (
        await db.execute(
            select(
                ManagedUser.server_id,
                ManagedUser.emby_user_id,
                ManagedUser.total_playback_seconds,
                ManagedUser.last_played_at,
            )
        )
    ).all()
    return {
        (int(server_id), str(emby_user_id)): {
            "hours": _hours(seconds),
            "last_played_at": last_played_at,
        }
        for server_id, emby_user_id, seconds, last_played_at in rows
    }


async def daily_trend(db: AsyncSession, days: int = 14) -> list[dict[str, object]]:
    """Return a compact daily playback series for the dashboard charts."""
    span = max(1, min(days, 365))
    since = utcnow() - timedelta(days=span - 1)
    rows = (
        await db.execute(
            select(
                func.date(PlaybackRecord.started_at),
                func.count(PlaybackRecord.id),
                func.sum(PlaybackRecord.watched_seconds),
            )
            .where(PlaybackRecord.started_at >= since)
            .group_by(func.date(PlaybackRecord.started_at))
            .order_by(func.date(PlaybackRecord.started_at))
        )
    ).all()
    values = {str(day): (int(plays or 0), _hours(seconds)) for day, plays, seconds in rows}
    start = since.date()
    return [
        {
            "date": (start + timedelta(days=index)).isoformat(),
            "plays": values.get((start + timedelta(days=index)).isoformat(), (0, 0))[0],
            "hours": values.get((start + timedelta(days=index)).isoformat(), (0, 0))[1],
        }
        for index in range(span)
    ]
