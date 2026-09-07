import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Request

from .config import get_settings

_settings = get_settings()

# admin_cookie: 管理端 8000 会话，浏览器 Cookie 名为 ec_session。
# portal_cookie: 用户端 8080 会话，浏览器 Cookie 名为 ec_portal。
ADMIN_COOKIE_NAME = "ec_session"
PORTAL_COOKIE_NAME = "ec_portal"


def _fernet() -> Fernet:
    """从主密钥派生一个稳定的 Fernet key，避免另存一份密钥文件。"""
    digest = hashlib.sha256(_settings.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError(
            "无法解密已保存的 API Key，数据目录中的 secret.key 可能已丢失或被替换，请重新录入服务器"
        ) from exc


def mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 8}{value[-4:]}"


# ---- 用户端口令 ----
# 用标准库 PBKDF2 而非 bcrypt/passlib：那两个依赖此前已被移除，
# 不值得为存几个用户密码重新引入编译型依赖。
_PBKDF2_ROUNDS = 240_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS
    )
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """格式不认识或为空一律返回 False，绝不因解析失败放行。"""
    if not stored:
        return False
    try:
        algorithm, rounds_text, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        rounds = int(rounds_text)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(candidate, expected)


# ---- 用户端会话 ----
# 与管理后台共用 SessionMiddleware 机制，但 key 不同，两边登录态不互通。
PORTAL_SESSION_KEY = "portal_user_id"


def portal_user_id(request: Request) -> int | None:
    value = request.session.get(PORTAL_SESSION_KEY)
    return value if isinstance(value, int) else None


def verify_admin(username: str, password: str) -> bool:
    """常量时间比对，避免用户名/密码长度或内容被时序推断。"""
    user_ok = hmac.compare_digest(username.encode(), _settings.admin_user.encode())
    pass_ok = hmac.compare_digest(password.encode(), _settings.admin_password.encode())
    return user_ok and pass_ok


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get("admin"))


def issue_csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def csrf_ok(request: Request, submitted: str | None) -> bool:
    expected = request.session.get("csrf")
    if not expected or not submitted:
        return False
    return hmac.compare_digest(expected.encode(), submitted.encode())
