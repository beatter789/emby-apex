import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from app.wecom_commands import (
    CommandRequest,
    MENU_EVENT_COMMANDS,
    begin_code_delete,
    clear_code_delete_sessions,
    consume_code_delete_reply,
    execute_quick,
    execute_mutating,
    is_async_command,
    limit_reply,
    parse_command,
)


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _QueryDb:
    def __init__(self, rows):
        self.rows = rows
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Rows(self.rows)


class _ScalarDb:
    def __init__(self, rows):
        self.rows = rows
        self.statement = None
        self.deleted = []

    async def scalars(self, statement):
        self.statement = statement
        return _Rows(self.rows)

    async def scalar(self, statement):
        self.statement = statement
        return self.rows[0] if self.rows else None

    async def delete(self, value):
        self.deleted.append(value)


class WeComCommandTests(unittest.TestCase):
    def tearDown(self):
        clear_code_delete_sessions()

    def test_text_commands(self):
        self.assertEqual(parse_command("状态"), CommandRequest("status"))
        self.assertEqual(parse_command("/status"), CommandRequest("status"))
        self.assertEqual(parse_command("users 测试"), CommandRequest("users", ("测试",)))
        self.assertEqual(parse_command("停用 Server/alice"), CommandRequest("disable", ("Server/alice",)))
        self.assertIsNone(parse_command("hello"))

    def test_menu_commands_and_async_classification(self):
        self.assertEqual(parse_command(event_key="cmd_status"), CommandRequest("status"))
        self.assertEqual(parse_command(event_key="sync"), CommandRequest("sync"))
        self.assertTrue(is_async_command(CommandRequest("sync")))
        self.assertFalse(is_async_command(CommandRequest("status")))
        self.assertEqual(parse_command(event_key="user_add"), CommandRequest("add_account"))
        self.assertEqual(parse_command("正在求片"), CommandRequest("request_pending"))
        self.assertTrue(is_async_command(CommandRequest("code_generate")))

    def test_all_menu_event_keys_have_one_command_mapping(self):
        expected = {
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
        self.assertEqual(MENU_EVENT_COMMANDS, expected)
        for event_key, command_name in expected.items():
            self.assertEqual(parse_command(event_key=event_key), CommandRequest(command_name))
        self.assertEqual(parse_command("增加用户"), CommandRequest("add_account"))
        self.assertEqual(parse_command("删除用户"), CommandRequest("delete_account"))
        self.assertEqual(parse_command("查询激活码"), CommandRequest("code_list"))
        self.assertEqual(parse_command("已求片"), CommandRequest("request_in_library"))
        self.assertIsNone(parse_command(event_key="unknown_event"))

    def test_in_library_request_query_filters_only_in_library_and_truncates(self):
        rows = [
            (SimpleNamespace(title=f"片名-{index}", tmdb_id=index), "alice", "主服务器")
            for index in range(30)
        ]
        db = _QueryDb(rows)
        result = asyncio.run(execute_quick(db, CommandRequest("request_in_library")))
        self.assertIn("已求片", result)
        self.assertIn("仅显示已入库", result)
        self.assertIn("仅显示前 25 条", result)
        self.assertIn("in_library", str(db.statement.compile().params.values()))

    def test_reply_size_limit_is_utf8_safe(self):
        reply = limit_reply("测" * 1000)
        self.assertLessEqual(len(reply.encode("utf-8")), 1800)
        self.assertIn("内容过长", reply)

    def test_interactive_code_delete_lists_only_unused_codes_with_stable_numbers(self):
        rows = [
            SimpleNamespace(
                id=3,
                code="NEW-CODE",
                amount=30,
                unit="day",
                created_at=datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc),
                used_at=None,
            ),
            SimpleNamespace(
                id=2,
                code="USED-CODE",
                amount=7,
                unit="day",
                created_at=datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
                used_at=datetime.now(timezone.utc),
            ),
            SimpleNamespace(
                id=1,
                code="OLD-CODE",
                amount=1,
                unit="hour",
                created_at=datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
                used_at=None,
            ),
        ]
        db = _ScalarDb(rows)
        result = asyncio.run(begin_code_delete(db, "alice"))
        self.assertIn("1、NEW-CODE", result)
        self.assertIn("2、OLD-CODE", result)
        self.assertNotIn("USED-CODE", result)
        self.assertIn("30 天", result)
        self.assertIn("创建于 2026-09-04", result)
        self.assertIn("used_at", str(db.statement.compile()))

    def test_interactive_code_delete_selection_requires_confirmation_and_cancel(self):
        rows = [SimpleNamespace(id=1, code="CODE-ONE", amount=30, unit="day", created_at=None, used_at=None)]
        db = _ScalarDb(rows)
        asyncio.run(begin_code_delete(db, "alice"))
        invalid = asyncio.run(consume_code_delete_reply(db, "alice", "9"))
        self.assertIsInstance(invalid, str)
        self.assertIn("序号无效", invalid)
        selected = asyncio.run(consume_code_delete_reply(db, "alice", "1"))
        self.assertIn("CODE-ONE", selected)
        self.assertIn("确认", selected)
        not_confirmed = asyncio.run(consume_code_delete_reply(db, "alice", "状态"))
        self.assertIn("确认", not_confirmed)
        cancelled = asyncio.run(consume_code_delete_reply(db, "alice", "取消"))
        self.assertEqual(cancelled, "已取消删除激活码。")
        self.assertIsNone(asyncio.run(consume_code_delete_reply(db, "alice", "确认")))

    def test_interactive_code_delete_confirm_returns_legacy_mutation_request(self):
        rows = [SimpleNamespace(id=1, code="CODE-ONE", amount=30, unit="day", created_at=None, used_at=None)]
        db = _ScalarDb(rows)
        asyncio.run(begin_code_delete(db, "alice"))
        asyncio.run(consume_code_delete_reply(db, "alice", "1"))
        request = asyncio.run(consume_code_delete_reply(db, "alice", "确认"))
        self.assertEqual(request, CommandRequest("code_delete", ("CODE-ONE", "确认")))

    def test_interactive_code_delete_expires_without_deleting(self):
        rows = [SimpleNamespace(id=1, code="CODE-ONE", amount=30, unit="day", created_at=None, used_at=None)]
        db = _ScalarDb(rows)
        asyncio.run(begin_code_delete(db, "alice"))
        import app.wecom_commands as commands_module

        commands_module._code_delete_sessions["alice"].expires_at = 0
        expired = asyncio.run(consume_code_delete_reply(db, "alice", "1"))
        self.assertIn("超时", expired)
        self.assertIsNone(asyncio.run(consume_code_delete_reply(db, "alice", "确认")))

    def test_interactive_code_delete_reply_isolated_by_userid(self):
        rows = [SimpleNamespace(id=1, code="CODE-ONE", amount=30, unit="day", created_at=None, used_at=None)]
        db = _ScalarDb(rows)
        asyncio.run(begin_code_delete(db, "alice"))
        self.assertIsNone(asyncio.run(consume_code_delete_reply(db, "bob", "1")))
        self.assertIn("CODE-ONE", asyncio.run(consume_code_delete_reply(db, "alice", "1")))

    def test_code_used_after_listing_is_rejected_by_legacy_mutation_guard(self):
        code = SimpleNamespace(
            id=1, code="CODE-ONE", amount=30, unit="day", created_at=None, used_at=None
        )
        db = _ScalarDb([code])
        asyncio.run(begin_code_delete(db, "alice"))
        asyncio.run(consume_code_delete_reply(db, "alice", "1"))
        code.used_at = datetime.now(timezone.utc)
        result = asyncio.run(execute_mutating(db, CommandRequest("code_delete", ("CODE-ONE", "确认"))))
        self.assertEqual(result, "已使用的激活码不能删除。")
        self.assertEqual(db.deleted, [])


if __name__ == "__main__":
    unittest.main()
