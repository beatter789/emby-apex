import asyncio
import logging
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings
from .models import Base

logger = logging.getLogger(__name__)
_settings = get_settings()

engine = create_async_engine(_settings.db_url, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
_init_lock = asyncio.Lock()
_initialized = False


@event.listens_for(engine.sync_engine, "connect")
def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    async def configure(connection) -> None:
        await connection.execute("PRAGMA foreign_keys=ON")
        await connection.execute("PRAGMA synchronous=FULL")
        await connection.execute("PRAGMA busy_timeout=5000")
        await connection.execute("PRAGMA wal_autocheckpoint=1000")

    # aiosqlite 的 SQLAlchemy 适配连接要求通过 run_async 调用底层连接。
    if hasattr(dbapi_connection, "run_async"):
        dbapi_connection.run_async(configure)
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA wal_autocheckpoint=1000")
    finally:
        cursor.close()

# create_all 只建新表，不会给已存在的表补列。老库升级靠这张清单。
# 格式：表名 -> [(列名, DDL 片段)]。加新列时往这里追加，不要写 migration 脚本。
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "servers": [
        ("is_register_target", "BOOLEAN NOT NULL DEFAULT 0"),
        ("library_json", "TEXT NOT NULL DEFAULT ''"),
    ],
    "managed_users": [
        ("portal_password_hash", "TEXT NOT NULL DEFAULT ''"),
        ("self_registered", "BOOLEAN NOT NULL DEFAULT 0"),
        ("registered_at", "DATETIME"),
        ("activated_at", "DATETIME"),
        ("playback_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
        ("playback_source", "VARCHAR(20) NOT NULL DEFAULT 'emby'"),
        ("policy_json", "TEXT NOT NULL DEFAULT ''"),
        ("playback_revoked_at", "DATETIME"),
        ("portal_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
        ("is_protected", "BOOLEAN NOT NULL DEFAULT 0"),
        ("claimed_at", "DATETIME"),
        ("claim_requested_at", "DATETIME"),
        ("activation_failed_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("activation_locked", "BOOLEAN NOT NULL DEFAULT 0"),
        ("activation_locked_at", "DATETIME"),
        ("total_playback_seconds", "FLOAT NOT NULL DEFAULT 0"),
        ("last_played_at", "DATETIME"),
    ],
    "redeem_codes": [
        ("owner_user_id", "INTEGER REFERENCES managed_users(id) ON DELETE CASCADE"),
        ("expires_at", "DATETIME"),
        ("source_bill_code", "VARCHAR(32)"),
    ],
    "bill_code_usages": [
        ("activation_notification_sent_at", "DATETIME"),
    ],
}


# 老库补列后要跑一次的数据回填。补列只能给出统一默认值，
# 这里把语义补上：已有的自助注册账户本就能登录 portal。
_BACKFILL: tuple[tuple[str, str], ...] = (
    (
        "UPDATE managed_users SET activation_failed_attempts = 5, "
        "activation_locked = 1, is_disabled = 1, disabled_by_controller = 1, "
        "playback_enabled = 0, activation_locked_at = COALESCE(activation_locked_at, CURRENT_TIMESTAMP) "
        "WHERE is_admin = 0 AND COALESCE(activation_failed_attempts, 0) >= 5",
        "已有达到失败上限的账户补齐账单码锁定状态",
    ),
    (
        "UPDATE managed_users SET activation_locked_at = COALESCE(activation_locked_at, CURRENT_TIMESTAMP) "
        "WHERE is_admin = 0 AND activation_locked = 1",
        "已有账单码锁定账户补齐锁定时间",
    ),
    (
        "UPDATE managed_users SET portal_enabled = 1 "
        "WHERE self_registered = 1 AND portal_enabled = 0",
        "已有自助注册账户补齐 portal 登录标记",
    ),
    (
        "UPDATE managed_users SET is_protected = 1 "
        "WHERE is_admin = 1 AND is_protected = 0",
        "管理员账号补齐受保护标记",
    ),
    (
        "UPDATE managed_users SET playback_source = 'controller' "
        "WHERE self_registered = 1 AND playback_source <> 'controller'",
        "自助注册账户补齐控制器播放权限来源",
    ),
    (
        "UPDATE managed_users SET playback_source = 'emby' "
        "WHERE self_registered = 0 AND claimed_at IS NOT NULL "
        "AND playback_source <> 'emby'",
        "认领账户补齐 Emby 播放权限来源",
    ),
)


async def _add_missing_columns(conn) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
        existing = {row[1] for row in result.fetchall()}
        if not existing:
            # 表还不存在，create_all 会按最新模型建好，无需补列
            continue
        for name, ddl in columns:
            if name in existing:
                continue
            await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            logger.info("数据库升级：%s 新增列 %s", table, name)
        if table == "managed_users" and "total_playback_seconds" not in existing:
            # Backfill only when adding the counter, before any history purge.
            await conn.exec_driver_sql(
                "UPDATE managed_users SET total_playback_seconds = COALESCE("
                "(SELECT SUM(watched_seconds) FROM playback_records p WHERE "
                "p.server_id = managed_users.server_id AND p.emby_user_id = managed_users.emby_user_id), 0), "
                "last_played_at = (SELECT MAX(started_at) FROM playback_records p WHERE "
                "p.server_id = managed_users.server_id AND p.emby_user_id = managed_users.emby_user_id)"
            )


async def _run_backfill(conn) -> None:
    """幂等：每条 UPDATE 都带 WHERE 排除已处理的行，重复启动不会改动已有数据。"""
    for sql, label in _BACKFILL:
        result = await conn.exec_driver_sql(sql)
        if result.rowcount:
            logger.info("数据库升级：%s（%s 行）", label, result.rowcount)


async def _ensure_indexes(conn) -> None:
    """补齐 create_all 不会为既有表创建的索引。"""
    try:
        await conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_media_request_pending "
            "ON media_requests (server_id, managed_user_id, tmdb_id, media_type) "
            "WHERE status = 'pending'"
        )
    except IntegrityError:
        # 极旧版本可能已经有重复进行中记录；保留服务层拦截，避免阻断整个升级。
        logger.warning("数据库升级：无法创建求片进行中唯一索引，已有重复记录需人工清理")
    # create_all does not add indexes/constraints for columns added above.
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_redeem_codes_owner_user_id ON redeem_codes (owner_user_id)"
    )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_redeem_codes_expires_at ON redeem_codes (expires_at)"
    )
    await conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_redeem_codes_source_bill_code "
        "ON redeem_codes (source_bill_code) WHERE source_bill_code IS NOT NULL"
    )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_playback_server_user "
        "ON playback_records (server_id, emby_user_id)"
    )


async def init_db() -> None:
    global _initialized
    if _initialized:
        return
    async with _init_lock:
        if _initialized:
            return
        async with engine.begin() as conn:
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            await conn.exec_driver_sql("PRAGMA synchronous=FULL")
            await conn.exec_driver_sql("PRAGMA busy_timeout=5000")
            await conn.exec_driver_sql("PRAGMA wal_autocheckpoint=1000")
            await conn.run_sync(Base.metadata.create_all)
            await _add_missing_columns(conn)
            await _ensure_indexes(conn)
            await _run_backfill(conn)

        # 建表之后再把后台配置读进内存缓存，业务代码随后同步读取。
        from . import settings_store

        async with SessionLocal() as session:
            await settings_store.load(session)
            from . import services

            await services.reset_open_playback_records(session)
            await services.purge_old_history(session)
        _initialized = True


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
