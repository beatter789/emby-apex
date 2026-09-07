import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRET_FILE_NAME = "secret.key"


class Settings(BaseSettings):
    # Startup settings intentionally use only the APEX_* namespace.  Runtime
    # operational settings continue to live in the database settings store.
    model_config = SettingsConfigDict(env_prefix="APEX_", env_file=".env", extra="ignore")

    # 密码不作长度或复杂度校验，公网部署时请自行改强。
    admin_user: str = Field(
        default="admin",
        validation_alias="APEX_USER",
    )
    admin_password: str = Field(
        default="admin",
        validation_alias="APEX_PASSWORD",
    )

    # 留空即由程序在 data_dir 下自动生成并持久化，无需手工准备。
    secret_key: str = ""

    data_dir: Path = Field(
        default=Path("/data"),
        validation_alias="APEX_DATA",
    )
    image_dir: Path = Field(
        default=Path("/image"),
        validation_alias="APEX_IMAGE",
    )
    timezone: str = "Asia/Shanghai"

    poll_interval_seconds: int = Field(default=20, ge=5, le=600)
    expiry_check_minutes: int = Field(default=5, ge=1, le=1440)
    expiry_remind_days: int = Field(default=3, ge=0, le=60)

    notify_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    cookie_secure: bool = Field(
        default=False,
        validation_alias="APEX_COOKIE",
    )
    session_max_age_seconds: int = 60 * 60 * 12

    # ---- 双端口 ----
    # 管理后台与用户端各跑一个进程，端口分开，便于只把 portal 端口暴露到公网。
    admin_port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        validation_alias="APEX_ADMIN_PORT",
    )
    portal_port: int = Field(
        default=8080,
        ge=1,
        le=65535,
        validation_alias="APEX_PORTAL_PORT",
    )
    # 关掉就只剩登录，注册页直接 404，用于临时停止放号。
    registration_enabled: bool = True
    # 未激活账户的保留时长：注册后未兑换任何码即到点删号。
    activation_grace_hours: int = Field(default=24, ge=1, le=720)
    # 到期先关播放，再过这些天才真正删号，留出续费窗口。
    purge_after_expiry_days: int = Field(default=7, ge=0, le=365)

    http_timeout_seconds: float = 12.0
    history_retention_days: int = 180

    @property
    def db_path(self) -> Path:
        return self.data_dir / "controller.db"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path.as_posix()}"

    @property
    def secret_file(self) -> Path:
        return self.data_dir / SECRET_FILE_NAME


def _ensure_data_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"数据目录 {path} 不可写（{exc}）。"
            "若使用 docker，请确认宿主机挂载目录存在且当前用户可写。"
        ) from exc


def _load_or_create_secret(path: Path) -> str:
    """密钥自动生成并落盘复用，保证重启后已存的 API Key 仍可解密。"""
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    generated = secrets.token_urlsafe(48)
    path.write_text(generated, encoding="utf-8")
    path.chmod(0o600)
    return generated


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    _ensure_data_dir(settings.data_dir)
    _ensure_data_dir(settings.image_dir)
    if not settings.secret_key:
        settings.secret_key = _load_or_create_secret(settings.secret_file)
    return settings
