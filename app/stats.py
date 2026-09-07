"""播放历史聚合查询。"""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import PlaybackRecord, utcnow


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
