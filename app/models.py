from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Server(Base):
    """一个受管的 Emby 服务器。api_key 以加密形式存储。"""

    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    base_url: Mapped[str] = mapped_column(String(400), nullable=False)
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    verify_ssl: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    server_version: Mapped[str | None] = mapped_column(String(60))
    # Emby 虚拟媒体库快照，JSON 数组 [{"id": "...", "name": "..."}]。
    # 读取失败时保留旧值，避免管理端空列表覆盖已有权限。
    library_json: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # 自助注册落在哪台服务器。同一时刻只允许一台为 True，由 services 保证。
    is_register_target: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    users: Mapped[list["ManagedUser"]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )


class ManagedUser(Base):
    """控制器对某台服务器上某个 Emby 用户的托管记录。"""

    __tablename__ = "managed_users"
    __table_args__ = (UniqueConstraint("server_id", "emby_user_id", name="uq_server_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    emby_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    username: Mapped[str] = mapped_column(String(200), nullable=False)

    is_disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # 手动停用与到期停用要区分开，续期时才知道该不该自动放行
    disabled_by_controller: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    expiry_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # 客户端限制：留空表示不限制
    client_policy_mode: Mapped[str] = mapped_column(String(10), default="off", nullable=False)
    client_policy_list: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # ---- 自助注册与用户端登录 ----
    # 由控制器创建的账户才有 portal 密码；Emby 上已存在的老用户为空，登录不了 portal。
    portal_password_hash: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # self_registered = 由自助注册通道创建。**只有它参与「未激活删号」。**
    # portal_enabled = 允许登录用户端。管理员为 Emby 已有账户「开通用户端」、
    # 或老用户自助认领成功时置位；认领账号另见 claimed_at。
    self_registered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    portal_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 受保护账户禁止删除（管理员账号同步时自动置位），防误删兜底。
    is_protected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # claimed_at = Emby 上已存在的老账号被「认领」的时刻（自助验原密码或管理员批准）。
    # 与 self_registered 互斥：认领账号不参与「未激活删号」，但**参与到期删号** ——
    # 前提是管理员给它设了 expires_at；没设到期时间即永久保留。
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 老账号在 Emby 上没设密码时无法验证身份，转管理员审批，这里记提交时刻。
    claim_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 未激活 = 从未兑换过任何码。24 小时内不激活由定时任务删号。
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    # 播放权限的本地影子状态，避免每轮都去 Emby 读 Policy 比对
    playback_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # controller：自助注册/管理员新建账号由控制器维护播放开关；
    # emby：老账号认领或管理员开通的已有账号跟随 Emby 实际策略。
    playback_source: Mapped[str] = mapped_column(String(20), default="emby", nullable=False)
    # 最近一次从 Emby 读取的完整 Policy，未知字段原样保留。
    policy_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 到期后先关播放，此刻起算 7 天再删号
    playback_revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )

    # 账户激活码错误尝试保护。达到第 5 次错误后锁定激活功能并停用账户。
    activation_failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    activation_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    activation_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 播放累计不随历史记录清理而归零，详情页展示账号生命周期累计值。
    total_playback_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    last_played_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    server: Mapped[Server] = relationship(back_populates="users")

    @property
    def client_patterns(self) -> list[str]:
        return [p.strip() for p in self.client_policy_list.split(",") if p.strip()]

    @client_patterns.setter
    def client_patterns(self, values: list[str]) -> None:
        self.client_policy_list = ",".join(v.strip() for v in values if v.strip())

    @property
    def is_activated(self) -> bool:
        return self.activated_at is not None

    @property
    def is_claimed(self) -> bool:
        """是否为被认领的 Emby 老账号（区别于控制器自助注册创建的账号）。"""
        return self.claimed_at is not None

    @property
    def claim_pending(self) -> bool:
        """已提交认领申请、等管理员批准（Emby 上没设密码，无法自助验证）。"""
        return self.claim_requested_at is not None and self.claimed_at is None

    @property
    def can_portal_login(self) -> bool:
        """能否登录用户端：自助注册的天然可以，老账户需管理员开通并设过密码。"""
        return bool(self.portal_password_hash) and (
            self.self_registered or self.portal_enabled
        )

    @property
    def deletable(self) -> bool:
        """管理员账号与受保护账号一律不可删。"""
        return not self.is_admin and not self.is_protected


class RedeemCode(Base):
    """兑换码。一码一用：used_by_user_id 落上就不能再兑。

    时长存成秒，界面上的「分/时/天/年」只是录入时的单位换算，
    这样查询和累加都不用再判断单位。
    """

    __tablename__ = "redeem_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True, index=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    # 原始录入值，仅用于界面回显「30 天」而不是「2592000 秒」
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    unit: Mapped[str] = mapped_column(String(10), nullable=False)

    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    # 兑换记录：谁、什么时候、兑完后到期时间变成多少
    used_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("managed_users.id", ondelete="SET NULL"), index=True
    )
    used_by_username: Mapped[str | None] = mapped_column(String(200))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resulting_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # 账单码兑换生成的待使用兑换码仅属于一个 portal 用户；管理员生成的码为空。
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("managed_users.id", ondelete="CASCADE"), index=True
    )
    # 兑换码本身的失效时间（与兑换后账户到期时间不同）。
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    source_bill_code: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)

    @property
    def is_used(self) -> bool:
        return self.used_at is not None


class BillCodeUsage(Base):
    """账单码成功使用记录；bill_code 唯一，确保不会重复发码。"""

    __tablename__ = "bill_code_usages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bill_code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("managed_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    redeem_code_id: Mapped[int] = mapped_column(
        ForeignKey("redeem_codes.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # Nullable so existing bill-code records can be notified after upgrade.
    activation_notification_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BillCodeDailyUsage(Base):
    """按用户与 Asia/Shanghai 自然日计数，唯一键配合原子 upsert 限制 3 次。"""

    __tablename__ = "bill_code_daily_usages"
    __table_args__ = (UniqueConstraint("user_id", "usage_date", name="uq_bill_daily_user_date"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("managed_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    usage_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class AppSetting(Base):
    """管理员在后台改的运行时配置，一行一个键。

    环境变量只作为首次启动的默认值，之后以本表为准。
    值统一存字符串，读取时由 settings_store 按类型转换。
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class MediaRequest(Base):
    """用户提交的电影/电视剧求片记录。"""

    __tablename__ = "media_requests"
    __table_args__ = (
        Index("ix_media_request_group", "server_id", "tmdb_id", "media_type", "status"),
        Index("ix_media_request_user_item", "server_id", "managed_user_id", "tmdb_id", "media_type"),
        Index(
            "uq_media_request_pending",
            "server_id",
            "managed_user_id",
            "tmdb_id",
            "media_type",
            unique=True,
            sqlite_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    managed_user_id: Mapped[int] = mapped_column(
        ForeignKey("managed_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tmdb_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    media_type: Mapped[str] = mapped_column(String(10), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    original_title: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    year: Mapped[int | None] = mapped_column(Integer)
    overview: Mapped[str] = mapped_column(Text, default="", nullable=False)
    poster_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    poster_local_path: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str | None] = mapped_column(String(200))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by: Mapped[str | None] = mapped_column(String(200))
    rejection_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    poster_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    media_source: Mapped[str] = mapped_column(String(50), default="tmdb", nullable=False)
    media_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    moviepilot_subscribe_ids: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    moviepilot_subscribe_state: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    library_state: Mapped[str] = mapped_column(String(20), default="unknown", nullable=False)
    library_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    moviepilot_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    season_numbers: Mapped[str] = mapped_column(Text, default="[]", nullable=False)


class MediaRequestSummary(Base):
    """终态求片的轻量摘要。

    详细请求按保留期清理后仍保留服务器级状态，用于已入库去重和展示。
    """

    __tablename__ = "media_request_summaries"
    __table_args__ = (
        UniqueConstraint(
            "server_id", "tmdb_id", "media_type", name="uq_media_request_summary_item"
        ),
        Index("ix_media_request_summary_status", "server_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tmdb_id: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(10), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    original_title: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    year: Mapped[int | None] = mapped_column(Integer)
    overview: Mapped[str] = mapped_column(Text, default="", nullable=False)
    poster_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    poster_local_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_by: Mapped[str | None] = mapped_column(String(200))
    rejection_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)


class PlaybackRecord(Base):
    """一次播放会话的聚合记录，在会话结束时从内存一次性写入。"""

    __tablename__ = "playback_records"
    __table_args__ = (
        UniqueConstraint("server_id", "session_key", name="uq_server_session"),
        Index("ix_playback_server_user", "server_id", "emby_user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_key: Mapped[str] = mapped_column(String(200), nullable=False)

    emby_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(200), nullable=False)

    item_id: Mapped[str | None] = mapped_column(String(64))
    item_name: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    series_name: Mapped[str | None] = mapped_column(String(500))
    item_type: Mapped[str | None] = mapped_column(String(60))

    client: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    device_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(200))
    remote_ip: Mapped[str | None] = mapped_column(String(64))
    play_method: Mapped[str | None] = mapped_column(String(60))

    position_ticks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    runtime_ticks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    watched_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActionLog(Base):
    """控制器做过的每一个动作，便于事后追查。"""

    __tablename__ = "action_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    level: Mapped[str] = mapped_column(String(10), default="info", nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    server_name: Mapped[str | None] = mapped_column(String(120))
    username: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
