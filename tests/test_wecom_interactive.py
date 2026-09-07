import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import wecom_commands as commands


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _FlowDb:
    def __init__(self):
        self.server = SimpleNamespace(id=1, name="主服务器", enabled=True)
        self.user = SimpleNamespace(
            id=2,
            server_id=1,
            username="alice",
            is_admin=False,
            is_disabled=False,
        )

    async def scalars(self, statement):
        text = str(statement)
        if "servers" in text:
            return _Rows([self.server])
        if "managed_users" in text:
            return _Rows([self.user])
        return _Rows([])


class _MultiFlowDb(_FlowDb):
    def __init__(self):
        super().__init__()
        self.server2 = SimpleNamespace(id=2, name="备用服务器", enabled=True)
        self.admin = SimpleNamespace(
            id=3,
            server_id=1,
            username="root",
            is_admin=True,
            is_disabled=False,
        )

    async def scalars(self, statement):
        text = str(statement)
        if "servers" in text:
            return _Rows([self.server, self.server2])
        if "managed_users" in text:
            return _Rows([self.user, self.admin])
        return _Rows([])


class WeComInteractiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        commands.clear_interaction_sessions()

    async def asyncTearDown(self):
        commands.clear_interaction_sessions()

    async def test_add_account_collects_parameters_without_one_shot_command(self):
        db = _FlowDb()
        prompt = await commands.begin_interaction(db, "alice-admin", "add_account")
        self.assertIn("服务器/用户名", prompt)
        self.assertIn("密码", await commands.consume_interaction(db, "alice-admin", "主服务器/new"))
        self.assertIn("播放", await commands.consume_interaction(db, "alice-admin", "pw-123"))
        self.assertIn("到期", await commands.consume_interaction(db, "alice-admin", "播放"))
        request = await commands.consume_interaction(db, "alice-admin", "永久")
        self.assertEqual(request, commands.CommandRequest("add_account", ("主服务器/new", "pw-123", "播放")))
        self.assertNotIn("alice-admin", commands._interaction_sessions)

    async def test_add_account_single_server_uses_default_and_numeric_options(self):
        db = _FlowDb()
        prompt = await commands.begin_interaction(db, "alice-admin", "add_account")
        self.assertIn("默认选择", prompt)
        self.assertIn("用户名", prompt)
        self.assertIn("密码", await commands.consume_interaction(db, "alice-admin", "newuser"))
        self.assertIn("1、允许播放", await commands.consume_interaction(db, "alice-admin", "pw-123"))
        self.assertIn("到期天数", await commands.consume_interaction(db, "alice-admin", "1"))
        request = await commands.consume_interaction(db, "alice-admin", "30")
        self.assertEqual(
            request,
            commands.CommandRequest(
                "add_account", ("主服务器/newuser", "pw-123", "播放", "到期天数=30")
            ),
        )

    async def test_add_account_multiple_servers_and_account_picker_excludes_admin(self):
        db = _MultiFlowDb()
        prompt = await commands.begin_interaction(db, "alice-admin", "add_account")
        self.assertIn("1、主服务器", prompt)
        self.assertIn("2、备用服务器", prompt)
        self.assertIn("用户名", await commands.consume_interaction(db, "alice-admin", "2"))
        self.assertIn("密码", await commands.consume_interaction(db, "alice-admin", "newuser"))

        listing = await commands.begin_interaction(db, "alice-admin", "delete_account")
        self.assertIn("1、主服务器/alice", listing)
        self.assertNotIn("root", listing)
        selected = await commands.consume_interaction(db, "alice-admin", "1")
        self.assertEqual(selected, commands.CommandRequest("delete_account", ("主服务器/alice",)))

    def test_split_reply_keeps_utf8_chunks_and_global_lines(self):
        chunks = commands.split_reply("\n".join(f"{index}、用户{index}" for index in range(1, 200)))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(item.encode("utf-8")) <= commands.MAX_REPLY_BYTES for item in chunks))
        self.assertEqual("\n".join(chunks).splitlines().count("1、用户1"), 1)

    async def test_direct_code_delete_selection_clears_canonical_session(self):
        code = SimpleNamespace(id=1, code="CODE-ONE", amount=30, unit="day", created_at=None, used_at=None)

        class Db:
            async def scalars(self, _statement):
                return _Rows([code])

        db = Db()
        listing = await commands.begin_interaction(db, "alice", "code_delete")
        self.assertIn("1、CODE-ONE", listing)
        result = await commands.consume_interaction(db, "alice", "1")
        self.assertEqual(result, commands.CommandRequest("code_delete", ("CODE-ONE",)))
        self.assertNotIn("alice", commands._interaction_sessions)
        self.assertNotIn("alice", commands._code_delete_sessions)

    async def test_request_picker_accepts_confirm_or_reject_reason(self):
        groups = [
            {
                "server_id": 1,
                "media_type": "movie",
                "tmdb_id": 9,
                "title": "测试电影",
                "server_name": "主服务器",
                "items": [{"username": "alice"}],
            }
        ]
        with patch.object(commands.services, "admin_media_request_groups", new=AsyncMock(return_value=groups)):
            await commands.begin_interaction(None, "alice", "request_pending")
        self.assertIn("确认", await commands.consume_interaction(None, "alice", "1"))
        accepted = await commands.consume_interaction(None, "alice", "确认")
        self.assertEqual(accepted, commands.CommandRequest("request_confirm", ("1", "movie", "9")))

        with patch.object(commands.services, "admin_media_request_groups", new=AsyncMock(return_value=groups)):
            await commands.begin_interaction(None, "alice", "request_pending")
        await commands.consume_interaction(None, "alice", "1")
        rejected = await commands.consume_interaction(None, "alice", "拒绝：重复求片")
        self.assertEqual(rejected, commands.CommandRequest("request_reject", ("1", "movie", "9", "重复求片")))

    async def test_cancel_restart_timeout_and_invalid_parameter(self):
        db = _FlowDb()
        await commands.begin_interaction(db, "alice", "enable_account")
        self.assertIn("格式错误", await commands.consume_interaction(db, "alice", "alice"))
        self.assertIn("取消", await commands.consume_interaction(db, "alice", "重新开始"))
        self.assertEqual(await commands.consume_interaction(db, "alice", "取消"), "已取消当前操作。")
        await commands.begin_interaction(db, "alice", "code_generate")
        commands._interaction_sessions["alice"].expires_at = 0
        self.assertIn("超时", await commands.consume_interaction(db, "alice", "1"))
