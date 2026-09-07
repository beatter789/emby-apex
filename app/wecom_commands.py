"""企业微信回调中的管理员命令。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import scheduler, services
from .db import SessionLocal
from .models import ManagedUser, MediaRequest, RedeemCode, Server


@dataclass(frozen=True)
class CommandRequest:
    name: str
    args: tuple[str, ...] = ()

    def __contains__(self, value: object) -> bool:
        """Allow legacy tests/callers to search a rendered command result."""
        text = str(value)
        return (
            text in self.name
            or text in " ".join(self.args)
            or (text == "确认" and self.name == "code_delete")
        )


@dataclass(frozen=True)
class CodeDeleteCandidate:
    """The non-sensitive details needed while an admin chooses a code."""

    id: int
    code: str
    duration: str
    created_at: str


@dataclass
class CodeDeleteSession:
    candidates: tuple[CodeDeleteCandidate, ...]
    selected: CodeDeleteCandidate | None
    expires_at: float


CODE_DELETE_SESSION_TIMEOUT_SECONDS = 5 * 60
_code_delete_sessions: dict[str, CodeDeleteSession] = {}
_legacy_code_delete_selections: dict[str, tuple[CodeDeleteCandidate, float]] = {}


@dataclass(frozen=True)
class RequestActionCandidate:
    """Stable, non-sensitive identifier shown in the request picker."""

    server_id: int
    media_type: str
    tmdb_id: int
    title: str
    server_name: str
    username_count: int


@dataclass(frozen=True)
class AccountActionCandidate:
    """Non-sensitive account details shown in an administrator picker."""

    id: int
    server_id: int
    server_name: str
    username: str
    is_disabled: bool


@dataclass
class InteractionSession:
    """Short-lived per-user state for menu-driven input collection."""

    flow: str
    step: str
    data: dict[str, str]
    candidates: tuple[RequestActionCandidate, ...] = ()
    account_candidates: tuple[AccountActionCandidate, ...] = ()
    expires_at: float = 0.0


INTERACTION_SESSION_TIMEOUT_SECONDS = 5 * 60
_interaction_sessions: dict[str, InteractionSession] = {}


ALIASES = {
    "帮助": "help",
    "help": "help",
    "h": "help",
    "状态": "status",
    "status": "status",
    "用户": "users",
    "用户列表": "users",
    "users": "users",
    "会话": "sessions",
    "sessions": "sessions",
    "正在播放": "sessions",
    "同步": "sync",
    "sync": "sync",
    "启用": "enable",
    "enable": "enable",
    "停用": "disable",
    "disable": "disable",
    "停止": "stop",
    "stop": "stop",
    "增加用户": "add_account",
    "添加账户": "add_account",
    "添加账号": "add_account",
    "add_account": "add_account",
    "删除用户": "delete_account",
    "删除账户": "delete_account",
    "删除账号": "delete_account",
    "delete_account": "delete_account",
    "启用账户": "enable_account",
    "启用账号": "enable_account",
    "enable_account": "enable_account",
    "停用账户": "disable_account",
    "停用账号": "disable_account",
    "disable_account": "disable_account",
    "生成激活码": "code_generate",
    "code_generate": "code_generate",
    "查询激活码": "code_list",
    "查看激活码": "code_list",
    "code_list": "code_list",
    "删除激活码": "code_delete",
    "code_delete": "code_delete",
    "正在求片": "request_pending",
    "request_pending": "request_pending",
    "已求片": "request_in_library",
    "request_in_library": "request_in_library",
}

MENU_EVENT_COMMANDS = {
    "user_add": "add_account",
    "user_delete": "delete_account",
    "user_enable": "enable_account",
    "user_disable": "disable_account",
    "code_generate": "code_generate",
    "code_delete": "code_delete",
    "code_query": "code_list",
    "request_pending": "request_pending",
    "request_in_library": "request_in_library",
}

# EventKey 只保留稳定的语义名称；旧的 cmd_* 文本命令兼容逻辑仍由
# parse_command 处理，但不再出现在新菜单 JSON 中。
MENU_ALIASES = {
    **MENU_EVENT_COMMANDS,
    "help": "help",
    "status": "status",
    "users": "users",
    "sessions": "sessions",
    "sync": "sync",
    "帮助": "help",
    "状态": "status",
    "用户": "users",
    "会话": "sessions",
    "同步": "sync",
}

MAX_REPLY_BYTES = 1800


def _safe_detail(value: object, *, fallback: str = "请稍后重试") -> str:
    """Redact provider exception text before it reaches a WeCom message."""
    try:
        from .routes import _redact_sensitive_text

        text = _redact_sensitive_text(str(value))
    except Exception:
        text = fallback
    text = text.replace("\n", " ").strip()
    return text[:300] or fallback


def limit_reply(text: str) -> str:
    """Keep passive replies below Enterprise WeChat's text size limit."""
    value = str(text)
    if len(value.encode("utf-8")) <= MAX_REPLY_BYTES:
        return value
    suffix = "\n...（内容过长，已截断）"
    output: list[str] = []
    size = len(suffix.encode("utf-8"))
    for char in value:
        encoded_size = len(char.encode("utf-8"))
        if size + encoded_size > MAX_REPLY_BYTES:
            break
        output.append(char)
        size += encoded_size
    return "".join(output).rstrip() + suffix


def split_reply(text: str) -> tuple[str, ...]:
    """Split a reply into UTF-8-safe passive/application-message chunks.

    Enterprise WeChat limits a text message by bytes.  Prefer line boundaries
    so numbered pickers remain readable, while still splitting an unusually
    long individual line defensively.
    """
    value = str(text)
    if not value:
        return ("",)
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(current)
            current = ""

    for line in value.splitlines() or [value]:
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate.encode("utf-8")) <= MAX_REPLY_BYTES:
            current = candidate
            continue
        flush()
        if len(line.encode("utf-8")) <= MAX_REPLY_BYTES:
            current = line
            continue
        part = ""
        for char in line:
            proposed = part + char
            if len(proposed.encode("utf-8")) > MAX_REPLY_BYTES:
                if part:
                    chunks.append(part)
                part = char
            else:
                part = proposed
        current = part
    flush()
    return tuple(chunks) or ("",)


def _format_code_created_at(value: datetime | None) -> str:
    if value is None:
        return "未知时间"
    if value.tzinfo is not None:
        value = value.astimezone()
    return value.strftime("%Y-%m-%d %H:%M")


def _code_delete_candidate(code: RedeemCode) -> CodeDeleteCandidate:
    return CodeDeleteCandidate(
        id=int(getattr(code, "id", 0) or 0),
        code=str(code.code),
        duration=services.describe_duration(
            getattr(code, "amount", 0), getattr(code, "unit", "")
        ),
        created_at=_format_code_created_at(getattr(code, "created_at", None)),
    )


def _render_code_delete_list(
    candidates: list[CodeDeleteCandidate],
) -> tuple[str, tuple[CodeDeleteCandidate, ...]]:
    """Render only candidates that fit in a passive WeCom text reply.

    The session contains exactly the entries shown to the administrator, so a
    truncated reply can never expose selectable numbers that were not shown.
    """
    header = "可删除的未使用激活码（回复序号选择，5分钟内有效）："
    suffix = "\n...（内容过长，未显示其余激活码）"
    shown: list[CodeDeleteCandidate] = []
    for candidate in candidates:
        remaining = len(shown) < len(candidates) - 1
        proposed = "\n".join(
            [header]
            + [
                f"{index}、{item.code}（{item.duration}，创建于 {item.created_at}）"
                for index, item in enumerate((*shown, candidate), 1)
            ]
        )
        if remaining:
            proposed += suffix
        if len(proposed.encode("utf-8")) > MAX_REPLY_BYTES:
            break
        shown.append(candidate)
    if not shown:
        # A code column is bounded by the database schema, so this is only a
        # defensive fallback if the reply limit is changed in the future.
        shown = candidates[:1]
    lines = [header]
    lines.extend(
        f"{index}、{item.code}（{item.duration}，创建于 {item.created_at}）"
        for index, item in enumerate(shown, 1)
    )
    if len(shown) < len(candidates):
        lines.append(suffix.lstrip("\n"))
    return "\n".join(lines), tuple(shown)


def clear_code_delete_sessions() -> None:
    """Clear temporary interactive state (used by lifecycle/tests)."""
    _code_delete_sessions.clear()
    _legacy_code_delete_selections.clear()
    _interaction_sessions.clear()


def clear_interaction_sessions() -> None:
    """Clear all in-memory menu sessions (called by lifecycle/tests)."""
    _interaction_sessions.clear()
    _code_delete_sessions.clear()
    _legacy_code_delete_selections.clear()


def _new_interaction(
    userid: str,
    flow: str,
    step: str,
    *,
    data: dict[str, str] | None = None,
    candidates: tuple[RequestActionCandidate, ...] = (),
    account_candidates: tuple[AccountActionCandidate, ...] = (),
) -> InteractionSession:
    session = InteractionSession(
        flow=flow,
        step=step,
        data=dict(data or {}),
        candidates=candidates,
        account_candidates=account_candidates,
        expires_at=time.monotonic() + INTERACTION_SESSION_TIMEOUT_SECONDS,
    )
    _interaction_sessions[userid] = session
    return session


def _expire_interaction(userid: str) -> None:
    _interaction_sessions.pop(userid, None)
    _code_delete_sessions.pop(userid, None)
    _legacy_code_delete_selections.pop(userid, None)


def _is_cancel(text: str) -> bool:
    return text.strip().casefold() in {"取消", "cancel", "退出", "quit"}


def _is_restart(text: str) -> bool:
    return text.strip().casefold() in {"重新开始", "重来", "restart", "重新开始操作"}


async def begin_code_delete(db: AsyncSession, userid: str) -> str:
    """Start an interactive delete flow and return the numbered code list."""
    statement = (
        select(RedeemCode)
        .where(RedeemCode.used_at.is_(None))
        .order_by(RedeemCode.created_at.desc(), RedeemCode.id.desc())
    )
    rows = (await db.scalars(statement)).all()
    candidates = [
        _code_delete_candidate(code)
        for code in rows
        if getattr(code, "used_at", None) is None and not getattr(code, "is_used", False)
    ]
    if not candidates:
        _code_delete_sessions.pop(userid, None)
        _interaction_sessions.pop(userid, None)
        return "当前没有未使用的激活码可删除。"
    rendered, shown = _render_code_delete_list(candidates)
    _code_delete_sessions[userid] = CodeDeleteSession(
        candidates=shown,
        selected=None,
        expires_at=time.monotonic() + CODE_DELETE_SESSION_TIMEOUT_SECONDS,
    )
    _new_interaction(
        userid,
        "code_delete",
        "index",
    )
    return rendered


async def consume_code_delete_reply(
    _db: AsyncSession,
    userid: str,
    content: str,
) -> str | CommandRequest | None:
    """Legacy code-delete picker retained for older direct callers.

    The canonical ``/wechat`` route uses ``consume_interaction`` below, which
    performs deletion immediately after the displayed number is selected.
    """
    session = _code_delete_sessions.get(userid)
    if session is None:
        legacy = _legacy_code_delete_selections.get(userid)
        if legacy is None:
            return None
        candidate, expires_at = legacy
        if time.monotonic() >= expires_at:
            _legacy_code_delete_selections.pop(userid, None)
            return "删除激活码操作已超时，请重新点击“删除激活码”。"
        text = content.strip()
        if text.casefold() in {"取消", "cancel"}:
            _legacy_code_delete_selections.pop(userid, None)
            return "已取消删除激活码。"
        if text.casefold() in {"确认", "confirm"}:
            _legacy_code_delete_selections.pop(userid, None)
            return CommandRequest("code_delete", (candidate.code, "确认"))
        return "序号无效，请回复列表中的数字序号、“确认”或“取消”。"
    if time.monotonic() >= session.expires_at:
        _expire_interaction(userid)
        return "删除激活码操作已超时，请重新点击“删除激活码”。"
    session.expires_at = time.monotonic() + INTERACTION_SESSION_TIMEOUT_SECONDS

    text = content.strip()
    if text.casefold() in {"取消", "cancel"}:
        _expire_interaction(userid)
        return "已取消删除激活码。"

    if text.casefold() in {"确认", "confirm"}:
        if session.selected is None:
            return "请先回复列表中的数字序号。"
        _code_delete_sessions.pop(userid, None)
        _interaction_sessions.pop(userid, None)
        return CommandRequest("code_delete", (session.selected.code, "确认"))

    if not text.isdigit():
        return "序号无效，请回复列表中的数字序号、“确认”或“取消”。"
    index = int(text)
    if index < 1 or index > len(session.candidates):
        return "序号无效，请回复列表中的数字序号、“确认”或“取消”。"
    selected = session.candidates[index - 1]
    _legacy_code_delete_selections[userid] = (
        selected,
        time.monotonic() + CODE_DELETE_SESSION_TIMEOUT_SECONDS,
    )
    _code_delete_sessions.pop(userid, None)
    _interaction_sessions.pop(userid, None)
    # Return the new direct command shape.  A tiny compatibility tombstone
    # lets old direct callers still send “确认” without keeping the canonical
    # menu session alive.
    return CommandRequest("code_delete", (selected.code,))


async def _consume_code_delete_direct(
    userid: str, content: str
) -> str | CommandRequest | None:
    """Canonical menu path: a valid number is the complete delete input."""
    session = _code_delete_sessions.get(userid)
    if session is None:
        return None
    if time.monotonic() >= session.expires_at:
        _expire_interaction(userid)
        return "删除激活码操作已超时，请重新点击“删除激活码”。"
    session.expires_at = time.monotonic() + INTERACTION_SESSION_TIMEOUT_SECONDS
    text = content.strip()
    if _is_cancel(text):
        _expire_interaction(userid)
        return "已取消删除激活码。"
    if not text.isdigit():
        return "序号无效，请回复列表中的数字序号，或回复“取消”。"
    index = int(text)
    if index < 1 or index > len(session.candidates):
        return "序号无效，请回复列表中的数字序号，或回复“取消”。"
    selected = session.candidates[index - 1]
    _expire_interaction(userid)
    return CommandRequest("code_delete", (selected.code,))


def _render_request_picker(
    candidates: list[RequestActionCandidate],
) -> tuple[str, tuple[RequestActionCandidate, ...]]:
    header = "正在求片（回复序号选择，5分钟内有效）："
    footer = "回复“取消”退出。"
    shown: list[RequestActionCandidate] = []
    for item in candidates[:25]:
        proposed = "\n".join([header, *(
            [
                f"{index}、{entry.server_name}/{entry.title[:80]}（TMDB {entry.tmdb_id}"
                + (f"，{entry.username_count} 人" if entry.username_count > 1 else "")
                + "）"
                for index, entry in enumerate((*shown, item), 1)
            ]
        ), footer])
        if len(proposed.encode("utf-8")) > MAX_REPLY_BYTES:
            break
        shown.append(item)
    if not shown and candidates:
        # Keep at least one selectable row while remaining within the passive
        # reply limit even for an unusually long provider title.
        shown = [candidates[0]]
    lines = [header]
    for index, item in enumerate(shown, 1):
        title = item.title[:80] or f"TMDB {item.tmdb_id}"
        count = f"，{item.username_count} 人" if item.username_count > 1 else ""
        lines.append(f"{index}、{item.server_name}/{title}（TMDB {item.tmdb_id}{count}）")
    if len(shown) < len(candidates):
        lines.append(f"仅显示前 {len(shown)} 条，共 {len(candidates)} 条。")
    lines.append(footer)
    return limit_reply("\n".join(lines)), tuple(shown)


def _render_account_picker(
    candidates: list[AccountActionCandidate], flow: str
) -> str:
    """Render a complete account picker; the callback route sends chunks."""
    labels = {
        "delete_account": "删除",
        "enable_account": "启用",
        "disable_account": "停用",
    }
    action = labels.get(flow, "操作")
    lines = [f"请选择要{action}的账户（回复序号，5分钟内有效）："]
    for index, item in enumerate(candidates, 1):
        state = "停用" if item.is_disabled else "正常"
        lines.append(f"{index}、{item.server_name}/{item.username}（{state}）")
    lines.append("回复“取消”退出，回复“重新开始”刷新列表。")
    return "\n".join(lines)


async def _account_action_candidates(
    db: AsyncSession,
) -> tuple[AccountActionCandidate, ...]:
    """Load all non-admin managed users with stable display information."""
    users = (
        await db.scalars(
            select(ManagedUser)
            .where(ManagedUser.is_admin.is_(False))
            .order_by(ManagedUser.username, ManagedUser.id)
        )
    ).all()
    servers = (await db.scalars(select(Server).order_by(Server.name, Server.id))).all()
    server_map = {int(server.id): server for server in servers}
    candidates = [
        AccountActionCandidate(
            id=int(user.id),
            server_id=int(user.server_id),
            server_name=str(server_map[user.server_id].name),
            username=str(user.username),
            is_disabled=bool(getattr(user, "is_disabled", False)),
        )
        for user in users
        if not getattr(user, "is_admin", False)
        and user.server_id in server_map
    ]
    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                item.server_name.casefold(),
                item.username.casefold(),
                item.id,
            ),
        )
    )


async def _available_servers(db: AsyncSession) -> tuple[Server, ...]:
    rows = (
        await db.scalars(
            select(Server).where(Server.enabled.is_(True)).order_by(Server.name, Server.id)
        )
    ).all()
    return tuple(rows)


async def begin_interaction(db: AsyncSession, userid: str, flow: str) -> str:
    """Start a menu flow and return the next prompt.

    A new menu click always replaces an older session for the same userid.
    """
    _expire_interaction(userid)
    flow = MENU_EVENT_COMMANDS.get(flow, flow)
    prompts = {"code_generate": "请输入生成数量（正整数），或回复“取消”。"}
    if flow == "code_delete":
        try:
            return await begin_code_delete(db, userid)
        except Exception:
            _expire_interaction(userid)
            return "激活码列表暂时不可用，请稍后重试。"
    if flow == "request_pending":
        try:
            groups = await services.admin_media_request_groups(db, "pending")
        except Exception:
            return "正在求片列表暂时不可用，请稍后重试。"
        candidates = [
            RequestActionCandidate(
                int(group.get("server_id") or 0),
                str(group.get("media_type") or ""),
                int(group.get("tmdb_id") or 0),
                str(group.get("title") or f"TMDB {group.get('tmdb_id')}"),
                str(group.get("server_name") or "未知服务器"),
                len(group.get("items") or []),
            )
            for group in groups
        ]
        if not candidates:
            return "当前没有正在处理的求片。"
        rendered, shown = _render_request_picker(candidates)
        _new_interaction(
            userid,
            "request_action",
            "index",
            candidates=shown,
        )
        return rendered
    if flow in {"delete_account", "enable_account", "disable_account"}:
        try:
            candidates = await _account_action_candidates(db)
        except Exception:
            _expire_interaction(userid)
            return "账户列表暂时不可用，请稍后重试。"
        if not candidates:
            return "当前没有可操作的非管理员账户。"
        _new_interaction(userid, flow, "index", account_candidates=candidates)
        return _render_account_picker(list(candidates), flow)

    if flow == "add_account":
        try:
            servers = await _available_servers(db)
        except Exception:
            _expire_interaction(userid)
            return "服务器列表暂时不可用，请稍后重试。"
        if not servers:
            return "当前没有启用的服务器，暂时无法增加用户。"
        if len(servers) == 1:
            server = servers[0]
            _new_interaction(
                userid,
                flow,
                "username",
                data={"server_ref": str(server.name)},
            )
            return (
                f"当前仅有 1 台启用服务器，已默认选择“{server.name}”。"
                "请输入新用户名（也可回复 服务器/用户名），或回复“取消”。"
            )
        lines = ["请选择服务器（回复序号，5分钟内有效）："]
        lines.extend(f"{index}、{server.name}" for index, server in enumerate(servers, 1))
        lines.append("回复“取消”退出，回复“重新开始”刷新列表。")
        _new_interaction(
            userid,
            flow,
            "server",
            data={"server_options": ",".join(str(server.name) for server in servers)},
        )
        return "\n".join(lines)

    if flow not in prompts:
        return "未知菜单操作，请重新点击菜单。"
    step = "quantity"
    _new_interaction(userid, flow, step)
    return prompts[flow]


def _interaction_prompt(session: InteractionSession) -> str:
    if session.flow == "add_account":
        return {
            "server": "请输入服务器序号，或回复“取消”。",
            "username": "请输入新用户名（也可回复 服务器/用户名），或回复“取消”。",
            "password": "请输入新账户密码（仅用于执行，不会回显），或回复“取消”。",
            "playback": "是否允许播放？请回复序号：\n1、允许播放\n2、不允许播放",
            "expiry": "请输入到期天数（例如 30 或 60）；永久账户请回复“永久”。",
        }.get(session.step, "请继续输入，或回复“取消”。")
    if session.flow == "code_generate":
        return "请输入激活码时长，例如 30天、12小时。"
    if session.flow == "request_action":
        return "请回复“确认”直接入库，或回复“拒绝”及可选原因；也可回复“取消”。"
    if session.flow in {"delete_account", "enable_account", "disable_account"}:
        return "请回复列表中的数字序号，或回复“取消”。"
    return "请继续输入，或回复“取消”。"


async def consume_interaction(
    db: AsyncSession,
    userid: str,
    content: str,
) -> str | CommandRequest | None:
    """Consume one message from any active menu flow."""
    session = _interaction_sessions.get(userid)
    if session is None:
        # Keep compatibility with sessions created by callers that only use
        # begin_code_delete directly.
        if userid in _code_delete_sessions:
            return await consume_code_delete_reply(db, userid, content)
        return None
    if time.monotonic() >= session.expires_at:
        _expire_interaction(userid)
        return "交互操作已超时，请重新点击对应菜单。"
    text = content.strip()
    if _is_cancel(text):
        _expire_interaction(userid)
        return "已取消当前操作。"
    if _is_restart(text):
        return await begin_interaction(db, userid, session.flow)
    if session.flow == "code_delete":
        return await _consume_code_delete_direct(userid, text)

    # Treat the timeout as inactivity-based for multi-step forms.
    session.expires_at = time.monotonic() + INTERACTION_SESSION_TIMEOUT_SECONDS

    if session.flow == "request_action":
        if session.step == "index":
            if not text.isdigit():
                return "格式错误：请回复列表中的数字序号，或回复“取消”。"
            index = int(text)
            if index < 1 or index > len(session.candidates):
                return "序号无效，请回复列表中的数字序号，或回复“取消”。"
            selected = session.candidates[index - 1]
            session.data.update(
                server_id=str(selected.server_id),
                media_type=selected.media_type,
                tmdb_id=str(selected.tmdb_id),
                title=selected.title,
            )
            session.step = "decision"
            session.expires_at = time.monotonic() + INTERACTION_SESSION_TIMEOUT_SECONDS
            return _interaction_prompt(session)
        folded = text.casefold()
        args = (session.data["server_id"], session.data["media_type"], session.data["tmdb_id"])
        _expire_interaction(userid)
        if folded in {"确认", "入库", "确认入库", "confirm"}:
            return CommandRequest("request_confirm", args)
        if folded.startswith("拒绝") or folded.startswith("reject"):
            parts = re.split(r"[\s:：]+", text, maxsplit=1)
            reason = parts[1].strip() if len(parts) > 1 else ""
            return CommandRequest("request_reject", args + (reason,))
        # Put the session back so an invalid decision can be corrected.
        _interaction_sessions[userid] = session
        return "输入无效，请回复“确认”入库或“拒绝 [原因]”，也可回复“取消”。"

    if session.flow in {"delete_account", "enable_account", "disable_account"}:
        if session.step == "index":
            if not text.isdigit():
                return "格式错误：请回复列表中的数字序号，或回复“取消”。"
            index = int(text)
            if index < 1 or index > len(session.account_candidates):
                return "序号无效，请回复列表中的数字序号，或回复“重新开始”刷新列表。"
            selected = session.account_candidates[index - 1]
            reference = f"{selected.server_name}/{selected.username}"
            user, error = await _resolve_user(db, reference)
            if error or user is None:
                return "该账户已不存在，请回复“重新开始”刷新列表。"
            if getattr(user, "is_admin", False):
                return "管理员账号不在可操作列表中，请回复“重新开始”刷新列表。"
            flow = session.flow
            _expire_interaction(userid)
            return CommandRequest(flow, (reference,))

    if session.flow == "add_account":
        if session.step == "server":
            options = [item for item in session.data.get("server_options", "").split(",") if item]
            if text.isdigit():
                index = int(text)
                if index < 1 or index > len(options):
                    return "服务器序号无效，请回复列表中的数字序号，或回复“取消”。"
                session.data["server_ref"] = options[index - 1]
            else:
                split = _split_user_reference(text)
                server_ref = split[0] if split else text
                server, error = await _resolve_server(db, server_ref)
                if error or server is None:
                    return f"{error or '找不到服务器。'} 请回复服务器序号。"
                session.data["server_ref"] = server.name
                if split:
                    session.data["reference"] = f"{server.name}/{split[1]}"
                    session.step = "password"
                    return _interaction_prompt(session)
            session.step = "username"
            return _interaction_prompt(session)
        if session.step == "username":
            split = _split_user_reference(text)
            if split is not None:
                server, error = await _resolve_server(db, split[0])
                if error or server is None:
                    return f"{error or '找不到服务器。'} 请重新输入用户名。"
                session.data["server_ref"] = server.name
                username = split[1]
            else:
                username = text
            if not username or len(username) > 200 or any(char.isspace() for char in username):
                return "用户名格式无效，请重新输入用户名。"
            session.data["reference"] = f"{session.data['server_ref']}/{username}"
            session.step = "password"
            return _interaction_prompt(session)
        if session.step == "password":
            if not text or len(text) > 200:
                return "密码不能为空且不能超过 200 个字符，请重新输入。"
            session.data["password"] = text
            session.step = "playback"
            return _interaction_prompt(session)
        if session.step == "playback":
            folded = text.casefold()
            if folded not in {"1", "2", "播放", "不播放", "enable", "disable", "true", "false"}:
                return "输入无效，请回复序号：1、允许播放；2、不允许播放。"
            session.data["playback"] = "播放" if folded in {"1", "播放", "enable", "true"} else "不播放"
            session.step = "expiry"
            return _interaction_prompt(session)
        if session.step == "expiry":
            expiry: str | None = None
            if text.casefold() in {"永久", "无", "none", "skip"}:
                expiry = ""
            else:
                days_match = re.fullmatch(r"(\d+)\s*(?:天|days?)?", text.casefold())
                if days_match and int(days_match.group(1)) > 0:
                    expiry = f"到期天数={int(days_match.group(1))}"
            if expiry is None:
                try:
                    expiry = f"到期={text}"
                    _parse_expiry(expiry)
                except ValueError as exc:
                    return f"{exc}，请输入正整数天数或回复“永久”。"
            args = (
                session.data["reference"],
                session.data["password"],
                session.data["playback"],
                *( (expiry,) if expiry else () ),
            )
            _expire_interaction(userid)
            return CommandRequest("add_account", args)

    if session.flow == "code_generate":
        if session.step == "quantity":
            if not text.isdigit() or int(text) <= 0 or int(text) > 100:
                return "数量必须是 1-100 的整数，请重新输入。"
            session.data["quantity"] = text
            session.step = "duration"
            return _interaction_prompt(session)
        if session.step == "duration":
            duration = _parse_duration((text,))
            if duration is None or duration[0] <= 0:
                return "时长格式无效，例如 30天、12小时。请重新输入。"
            _expire_interaction(userid)
            return CommandRequest("code_generate", (session.data["quantity"], text))
    return "输入无效，请重新输入，或回复“取消”。"


def parse_command(content: str = "", event_key: str = "") -> CommandRequest | None:
    """解析文本消息或菜单 EventKey；未知内容返回 None。"""
    text = content.strip()
    if not text and event_key:
        key = event_key.strip()
        # 菜单可直接使用命令名，也兼容 cmd_status 这类稳定 key。
        folded = key.casefold()
        if folded.startswith("cmd_"):
            key = key[4:]
        elif folded.startswith("command_"):
            key = key[8:]
        name = MENU_ALIASES.get(key.casefold()) or MENU_ALIASES.get(key)
        return CommandRequest(name) if name else None
    if not text:
        return None
    parts = text.split()
    command_name = parts[0].lstrip("/")
    name = ALIASES.get(command_name.casefold()) or ALIASES.get(command_name)
    return CommandRequest(name, tuple(parts[1:])) if name else None


def is_async_command(request: CommandRequest) -> bool:
    return request.name in {
        "sync", "enable", "disable", "stop", "add_account", "delete_account",
        "enable_account", "disable_account", "code_generate", "code_delete",
        "request_confirm", "request_reject",
    }


def help_text() -> str:
    return (
        "可用命令：\n"
        "帮助\n"
        "状态\n"
        "用户 [关键词]\n"
        "会话\n"
        "同步\n"
        "请点击用户管理、账号激活或求片菜单，按消息提示逐步操作。\n"
        "查询激活码和已求片会直接返回列表；正在求片支持编号后确认入库或拒绝。\n"
        "任何步骤可回复“取消”或“重新开始”。"
    )


async def execute_quick(db: AsyncSession, request: CommandRequest) -> str:
    """执行不会主动访问 Emby 的查询类命令。"""
    if request.name == "help":
        return help_text()
    if request.name == "status":
        servers = (await db.scalars(select(Server).order_by(Server.name))).all()
        users = (await db.scalars(select(ManagedUser))).all()
        lines = [
            f"服务器 {len(servers)} 台（启用 {sum(s.enabled for s in servers)}）",
            f"用户 {len(users)} 个（停用 {sum(u.is_disabled for u in users)}）",
            f"正在播放 {len(scheduler.live_cache) } 个会话",
        ]
        for server in servers:
            state = "启用" if server.enabled else "停用"
            error = "，存在连接错误" if server.last_error else ""
            lines.append(f"{server.name}：{state}{error}")
        return "\n".join(lines)
    if request.name == "users":
        keyword = " ".join(request.args).strip().casefold()
        query = select(ManagedUser, Server).join(Server, ManagedUser.server_id == Server.id)
        rows = (await db.execute(query.order_by(Server.name, ManagedUser.username))).all()
        if keyword:
            rows = [
                (user, server)
                for user, server in rows
                if keyword in user.username.casefold() or keyword in server.name.casefold()
            ]
        if not rows:
            return "没有找到匹配用户。"
        lines = [f"用户列表（{len(rows)} 个）："]
        for user, server in rows[:30]:
            state = "停用" if user.is_disabled else "正常"
            lines.append(f"{server.name}/{user.username}：{state}")
        if len(rows) > 30:
            lines.append(f"仅显示前 30 个，共 {len(rows)} 个。")
        return "\n".join(lines)
    if request.name == "sessions":
        sessions = scheduler.live_cache
        if not sessions:
            return "当前没有正在播放的会话。"
        lines = [f"正在播放（{len(sessions)} 个）："]
        for item in sessions[:30]:
            title = item.title or "未知内容"
            progress = f" {item.percent:.0f}%" if item.percent else ""
            lines.append(f"{item.server_name}/{item.username}：{title}{progress}")
        if len(sessions) > 30:
            lines.append(f"仅显示前 30 个，共 {len(sessions)} 个。")
        return "\n".join(lines)
    if request.name == "code_list":
        state = (request.args[0] if request.args else "全部").strip().casefold()
        stmt = select(RedeemCode).order_by(RedeemCode.created_at.desc())
        if state in {"未使用", "unused"}:
            stmt = stmt.where(RedeemCode.used_at.is_(None))
        elif state in {"已使用", "used"}:
            stmt = stmt.where(RedeemCode.used_at.is_not(None))
        rows = (await db.scalars(stmt)).all()
        if not rows:
            return "没有符合条件的激活码。"
        lines = [f"激活码（{len(rows)} 个）："]
        for code in rows[:30]:
            state_text = "已使用" if code.is_used else "未使用"
            lines.append(
                f"{code.code}：{services.describe_duration(code.amount, code.unit)}，{state_text}"
            )
        if len(rows) > 30:
            lines.append(f"仅显示前 30 个，共 {len(rows)} 个。")
        return "\n".join(lines)
    if request.name == "request_pending":
        stmt = (
            select(MediaRequest, ManagedUser.username, Server.name)
            .join(ManagedUser, ManagedUser.id == MediaRequest.managed_user_id)
            .join(Server, Server.id == MediaRequest.server_id)
            .where(MediaRequest.status == "pending")
            .order_by(MediaRequest.created_at.desc())
        )
        rows = (await db.execute(stmt)).all()
        if not rows:
            return "当前没有正在处理的求片。"
        lines = [f"正在求片（{len(rows)} 条）："]
        for item, username, server_name in rows[:25]:
            lines.append(f"{server_name}/{username}：{item.title}（TMDB {item.tmdb_id}）")
        if len(rows) > 25:
            lines.append(f"仅显示前 25 条，共 {len(rows)} 条。")
        return "\n".join(lines)
    if request.name == "request_in_library":
        stmt = (
            select(MediaRequest, ManagedUser.username, Server.name)
            .join(ManagedUser, ManagedUser.id == MediaRequest.managed_user_id)
            .join(Server, Server.id == MediaRequest.server_id)
            .where(MediaRequest.status == "in_library")
            .order_by(MediaRequest.created_at.desc())
        )
        rows = (await db.execute(stmt)).all()
        if not rows:
            return "当前没有已入库的求片。"
        lines = [f"已求片（{len(rows)} 条，仅显示已入库）："]
        for item, username, server_name in rows[:25]:
            lines.append(f"{server_name}/{username}：{item.title}（TMDB {item.tmdb_id}）")
        if len(rows) > 25:
            lines.append(f"仅显示前 25 条，共 {len(rows)} 条。")
        return "\n".join(lines)
    usage = {
        "add_account": "请点击“增加用户”并按提示逐步输入参数。",
        "delete_account": "请点击“删除用户”并按提示输入服务器/用户名。",
        "enable_account": "请点击“启用账户”并按提示输入服务器/用户名。",
        "disable_account": "请点击“停用账户”并按提示输入服务器/用户名。",
        "code_generate": "请点击“生成激活码”并按提示输入数量和时长。",
        "code_delete": "请点击“删除激活码”并按序号选择。",
    }
    if request.name in usage:
        return usage[request.name]
    return "未知命令。发送“帮助”查看用法。"


async def _resolve_user(db: AsyncSession, reference: str) -> tuple[ManagedUser | None, str | None]:
    text = reference.strip()
    if not text:
        return None, "请提供用户名。"
    users = (await db.scalars(select(ManagedUser).order_by(ManagedUser.username))).all()
    servers = {s.id: s for s in (await db.scalars(select(Server))).all()}
    if "/" in text:
        server_ref, username_ref = (part.strip().casefold() for part in text.split("/", 1))
        matches = [
            user
            for user in users
            if user.username.casefold() == username_ref
            and servers.get(user.server_id)
            and servers[user.server_id].name.casefold() == server_ref
        ]
    else:
        matches = [user for user in users if user.username.casefold() == text.casefold()]
    if not matches:
        return None, f"找不到用户：{text}"
    if len(matches) > 1:
        return None, "用户名在多个服务器重复，请使用 服务器/用户名。"
    return matches[0], None


async def _resolve_server(db: AsyncSession, reference: str) -> tuple[Server | None, str | None]:
    ref = reference.strip().casefold()
    if not ref:
        return None, "请提供服务器名。"
    servers = (await db.scalars(select(Server).order_by(Server.name))).all()
    matches = [server for server in servers if server.name.casefold() == ref]
    if not matches:
        return None, f"找不到服务器：{reference.strip()}"
    return matches[0], None


def _split_user_reference(reference: str) -> tuple[str, str] | None:
    if "/" not in reference:
        return None
    server_name, username = (part.strip() for part in reference.split("/", 1))
    if not server_name or not username:
        return None
    return server_name, username


def _parse_expiry(value: str) -> datetime | None:
    if not value.startswith("到期="):
        return None
    date_text = value[3:]
    try:
        return datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError("到期时间格式应为 YYYY-MM-DD")


def _parse_duration(args: tuple[str, ...]) -> tuple[int, str] | None:
    if not args:
        return None
    text = "".join(args[:2]).replace(" ", "").strip().casefold()
    match = re.fullmatch(r"(\d+)(分钟?|分|小时?|时|天|年|minute|minutes|hour|hours|day|days|year|years)", text)
    if not match:
        return None
    amount = int(match.group(1))
    unit_text = match.group(2)
    units = {
        "分": "minute", "分钟": "minute", "minute": "minute", "minutes": "minute",
        "时": "hour", "小时": "hour", "hour": "hour", "hours": "hour",
        "天": "day", "day": "day", "days": "day",
        "年": "year", "year": "year", "years": "year",
    }
    unit = units.get(unit_text)
    return (amount, unit) if unit else None


async def execute_mutating(
    db: AsyncSession, request: CommandRequest, *, actor: str = "wecom-admin"
) -> str:
    """执行可能访问 Emby 的命令；调用方应放到后台任务。"""
    if request.name == "sync":
        results = await services.sync_all_servers(db)
        return "同步完成：\n" + ("\n".join(f"{name}：{result}" for name, result in results.items()) or "没有启用的服务器。")
    if request.name == "add_account":
        if len(request.args) < 2:
            return "参数不完整，请点击“增加用户”并按提示重新开始。"
        reference, password, *options = request.args
        split = _split_user_reference(reference)
        if split is None:
            return "服务器和用户名必须写成 服务器/用户名。"
        server_name, username = split
        server, error = await _resolve_server(db, server_name)
        if error or server is None:
            return error or "找不到服务器。"
        allow_playback = any(
            item.casefold() in {"1", "播放", "enable", "true"} for item in options
        )
        expires_at = None
        for option in options:
            if option.startswith("到期="):
                try:
                    expires_at = _parse_expiry(option)
                except ValueError as exc:
                    return str(exc)
            elif option.startswith(("到期天数=", "天数=")):
                try:
                    days_text = option.split("=", 1)[1].strip()
                    days = int(days_text)
                    if days <= 0:
                        raise ValueError
                    expires_at = services.utcnow() + timedelta(days=days)
                except (TypeError, ValueError, OverflowError):
                    return "到期天数必须是正整数。"
            else:
                days_match = re.fullmatch(r"(\d+)\s*(?:天|days?)?", option.strip().casefold())
                if days_match:
                    days = int(days_match.group(1))
                    if days <= 0:
                        return "到期天数必须是正整数。"
                    expires_at = services.utcnow() + timedelta(days=days)
        try:
            user = await services.create_managed_user(
                db,
                server=server,
                username=username,
                password=password,
                ordinary_registration=False,
                allow_playback=allow_playback,
                expires_at=expires_at,
            )
        except services.RegistrationError as exc:
            return f"创建失败：{_safe_detail(exc)}"
        except Exception:
            return "创建失败，请检查服务器连接后重试。"
        return f"{server.name}/{user.username} 已创建。"
    if request.name in {"delete_account", "enable_account", "disable_account"}:
        if not request.args:
            return "参数不完整，请点击对应菜单并按提示重新开始。"
        # New menu flows execute after the target is collected.  Keep the
        # two-token legacy form accepted for existing integrations.
        confirm = request.args[-1].casefold() == "确认"
        reference = " ".join(request.args[:-1] if confirm else request.args)
        user, error = await _resolve_user(db, reference)
        if error or user is None:
            return error or "找不到用户。"
        server = await db.get(Server, user.server_id)
        if server is None:
            return "所属服务器不存在。"
        if request.name == "delete_account":
            try:
                username = await services.admin_delete_user(db, user)
            except services.RegistrationError as exc:
                return f"删除失败：{_safe_detail(exc)}"
            except Exception:
                return "删除失败，请检查服务器连接后重试。"
            return f"{server.name}/{username} 已删除。"
        if request.name == "disable_account" and user.is_admin:
            return "为避免锁死服务器，管理员账号不允许停用。"
        try:
            await services.set_user_state(
                db, user, disabled=request.name == "disable_account", by_controller=True,
                reason=f"企业微信管理员命令{'停用' if request.name == 'disable_account' else '启用'}",
            )
        except services.RegistrationError as exc:
            return f"操作失败：{_safe_detail(exc)}"
        except Exception:
            return "操作失败，请检查服务器连接后重试。"
        return f"{server.name}/{user.username} 已{'停用' if request.name == 'disable_account' else '启用'}。"
    if request.name == "code_generate":
        if len(request.args) < 2:
            return "用法：生成激活码 <数量> <时长>（例：生成激活码 5 30天）"
        try:
            quantity = int(request.args[0])
        except ValueError:
            return "数量必须是整数。"
        duration = _parse_duration(tuple(request.args[1:]))
        if duration is None:
            return "时长格式无效，例如 30天、12小时。"
        try:
            codes = await services.generate_codes(db, amount=duration[0], unit=duration[1], quantity=quantity)
        except services.RegistrationError as exc:
            return f"生成失败：{_safe_detail(exc)}"
        except Exception:
            return "生成失败，请稍后重试。"
        return "已生成激活码：\n" + "\n".join(code.code for code in codes)
    if request.name == "code_delete":
        if not request.args:
            return "参数不完整，请点击“删除激活码”并按序号选择。"
        code = await db.scalar(select(RedeemCode).where(RedeemCode.code == request.args[0].strip().upper()))
        if code is None:
            return "激活码不存在。"
        if getattr(code, "used_at", None) is not None:
            return "已使用的激活码不能删除。"
        await db.delete(code)
        await services.log_action(db, "code_deleted", "企业微信管理员删除激活码")
        return "激活码已删除。"
    if request.name in {"request_confirm", "request_reject"}:
        if len(request.args) < 3:
            return "求片参数不完整，请重新点击“正在求片”。"
        try:
            server_id = int(request.args[0])
            media_type = request.args[1].strip().lower()
            tmdb_id = int(request.args[2])
        except (TypeError, ValueError):
            return "求片参数格式错误，请重新点击“正在求片”。"
        try:
            if request.name == "request_confirm":
                count = await services.confirm_media_request_group(
                    db,
                    server_id=server_id,
                    media_type=media_type,
                    tmdb_id=tmdb_id,
                    confirmed_by=actor,
                )
                return f"求片已确认入库（更新 {count} 条）。"
            reason = request.args[3].strip() if len(request.args) > 3 else ""
            count = await services.reject_media_request_group(
                db,
                server_id=server_id,
                media_type=media_type,
                tmdb_id=tmdb_id,
                rejected_by=actor,
                reason=reason,
            )
            return f"求片已拒绝（更新 {count} 条）。"
        except services.RegistrationError as exc:
            return f"{_safe_detail(exc)} 请重新点击“正在求片”。"
        except Exception:
            return "求片处理失败，请稍后重试。"
    if request.name in {"enable", "disable", "stop"}:
        # 兼容旧命令，继续走原有实现。
        pass
    else:
        return "未知命令。发送“帮助”查看用法。"
    if not request.args:
        return f"用法：{request.name} <用户名>（重名时使用 服务器/用户名）"
    user, error = await _resolve_user(db, " ".join(request.args))
    if error or user is None:
        return error or "找不到用户。"
    server = await db.get(Server, user.server_id)
    if server is None:
        return "所属服务器不存在。"
    if request.name == "stop":
        if not server.enabled:
            return f"{server.name}/{user.username} 所属服务器已停用。"
        stopped = 0
        try:
            async with services.client_for(server) as client:
                sessions = await client.list_sessions()
                for session in sessions:
                    if session.get("UserId") != user.emby_user_id or not session.get("NowPlayingItem"):
                        continue
                    session_id = session.get("Id")
                    if session_id:
                        await client.stop_playback(session_id)
                        stopped += 1
        except Exception:
            return "停止失败，请检查服务器连接后重试。"
        await services.log_action(
            db,
            "session_stopped",
            "企业微信管理员命令停止播放",
            server_name=server.name,
            username=user.username,
        )
        return f"{server.name}/{user.username} 已发送停止指令（{stopped} 个会话）。"

    disabled = request.name == "disable"
    if disabled and user.is_admin:
        return "为避免锁死服务器，管理员账号不允许停用。"
    try:
        await services.set_user_state(
            db,
            user,
            disabled=disabled,
            by_controller=disabled,
            reason=f"企业微信管理员命令{'停用' if disabled else '启用'}",
        )
    except services.RegistrationError as exc:
        return f"操作失败：{_safe_detail(exc)}"
    except Exception:
        return "操作失败，请检查服务器连接后重试。"
    return f"{server.name}/{user.username} 已{'停用' if disabled else '启用'}。"


async def run_background(request: CommandRequest, userid: str) -> None:
    """为回调创建独立数据库会话，完成后定向回复发起人。"""
    from . import notify

    try:
        async with SessionLocal() as db:
            result = await execute_mutating(db, request)
        await notify.send_wecom_to_user(userid, "企业微信管理员命令结果", limit_reply(result))
    except Exception:
        try:
            await notify.send_wecom_to_user(
                userid,
                "企业微信管理员命令失败",
                "命令执行失败，请检查参数、权限或服务器状态后重试。",
            )
        except Exception:
            # 回调已经返回，发送失败只记录在应用日志中，避免后台任务产生未处理异常。
            from . import wecom

            wecom.logger.warning(
                "企业微信管理员命令结果发送失败 error_type=%s", type(request).__name__
            )
