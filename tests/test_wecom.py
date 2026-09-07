import base64
import struct
import unittest
import logging
import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

from app.wecom import (
    APPLICATION_MENU,
    WeComClient,
    WeComConfig,
    callback_signature,
    callback_timestamp_is_fresh,
    decrypt_callback,
    encrypt_callback,
    encrypted_text_reply,
    is_api_base_url,
    parse_admin_whitelist,
    parse_message_xml,
    validate_menu,
    validate_encoding_aes_key,
    validate_token,
    verify_callback_signature,
)
from app.wecom_commands import MENU_EVENT_COMMANDS
from app import notify, settings_store


class WeComTests(unittest.IsolatedAsyncioTestCase):
    def test_api_base_url_and_menu_shape(self):
        self.assertTrue(is_api_base_url("https://qyapi.weixin.qq.com/"))
        self.assertFalse(is_api_base_url("http://127.0.0.1:7890"))
        buttons = APPLICATION_MENU["button"]
        self.assertEqual([button["name"] for button in buttons], ["用户管理", "账号激活", "求片"])
        self.assertEqual([len(button["sub_button"]) for button in buttons], [4, 3, 2])
        menu_items = [item for button in buttons for item in button["sub_button"]]
        self.assertEqual(len(menu_items), 9)
        self.assertEqual({item["key"] for item in menu_items}, set(MENU_EVENT_COMMANDS))
        self.assertEqual(len({item["key"] for item in menu_items}), len(menu_items))
        for button in buttons:
            self.assertLessEqual(len(button["name"].encode("utf-8")), 16)
            self.assertNotIn("type", button)
            for item in button["sub_button"]:
                self.assertEqual(item["type"], "click")
                self.assertLessEqual(len(item["name"].encode("utf-8")), 40)
                self.assertLessEqual(len(item["key"].encode("utf-8")), 128)
                self.assertTrue(item["key"].replace("_", "").isalnum())
        client = WeComClient(
            WeComConfig(
                corp_id="wwcorp",
                agent_id=100,
                secret="secret",
                api_base_url="https://relay.example.com/wecom",
            )
        )
        self.assertEqual(client.config.api_base_url, "https://relay.example.com/wecom")
        self.assertEqual(
            WeComClient._validate_api_base_url("https://relay.example.com/api"),
            "https://relay.example.com/api",
        )

    def test_callback_crypto_and_validation(self):
        key = bytes(range(32))
        aes_key = base64.b64encode(key).decode("ascii").rstrip("=")
        message = "<xml><FromUserName><![CDATA[alice]]></FromUserName></xml>"
        plain = b"0123456789abcdef" + struct.pack("!I", len(message.encode()))
        plain += message.encode() + b"wwcorp"
        padder = padding.PKCS7(256).padder()
        padded = padder.update(plain) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
        encrypted = base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()

        signature = callback_signature("token", "100", "nonce", encrypted)
        self.assertTrue(verify_callback_signature("token", "100", "nonce", encrypted, signature))
        self.assertFalse(verify_callback_signature("token", "100", "nonce", encrypted, "bad"))
        self.assertTrue(callback_timestamp_is_fresh("100", max_age_seconds=10**12))
        self.assertFalse(callback_timestamp_is_fresh("not-a-timestamp"))
        self.assertEqual(decrypt_callback(aes_key, encrypted), (message, "wwcorp"))
        self.assertEqual(parse_admin_whitelist(" alice, bob,alice , "), ("alice", "bob"))
        self.assertEqual(validate_token("abc123"), "abc123")
        self.assertEqual(validate_encoding_aes_key(aes_key), aes_key)

    async def test_access_token_cache_and_text_payload(self):
        WeComClient._token_cache.clear()
        WeComClient._token_locks.clear()
        client = WeComClient(
            WeComConfig(
                corp_id="wwcorp",
                agent_id=100,
                secret="secret",
                admin_whitelist=("alice", "bob"),
            )
        )
        calls = []

        async def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if path == "/cgi-bin/gettoken":
                return {"errcode": 0, "access_token": "access-1", "expires_in": 7200}
            return {"errcode": 0, "errmsg": "ok"}

        client._request_json = fake_request
        async with client:
            await client.send_text("标题", "正文")
            await client.send_text("标题2", "正文2")

        token_calls = [item for item in calls if item[1] == "/cgi-bin/gettoken"]
        send_calls = [item for item in calls if item[1] == "/cgi-bin/message/send"]
        self.assertEqual(len(token_calls), 1)
        self.assertEqual(len(send_calls), 2)
        payload = send_calls[0][2]["json"]
        self.assertEqual(payload["touser"], "@all")
        self.assertEqual(payload["agentid"], 100)
        self.assertEqual(payload["text"]["content"], "标题\n正文")

        calls.clear()
        async with WeComClient(client.config) as fresh_client:
            fresh_client._request_json = fake_request
            await fresh_client.send_text("定向", "正文", recipients=("alice",))
        directed = [item for item in calls if item[1] == "/cgi-bin/message/send"][0][2]["json"]
        self.assertEqual(directed["touser"], "alice")

    async def test_menu_api_and_notification_routing(self):
        client = WeComClient(WeComConfig(corp_id="wwcorp", agent_id=100, secret="secret"))
        calls = []

        async def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if path == "/cgi-bin/gettoken":
                return {"errcode": 0, "access_token": "access-1", "expires_in": 7200}
            return {"errcode": 0}

        client._request_json = fake_request
        async with client:
            await client.create_menu(APPLICATION_MENU)
            await client.get_menu()
            await client.delete_menu()
        for _method, path, kwargs in calls:
            if "menu/" in path:
                self.assertEqual(kwargs["params"]["agentid"], 100)

        original = settings_store.current()
        settings_store._cache = original.__class__(
            **{
                **original.__dict__,
                "wecom_corp_id": "wwcorp",
                "wecom_agent_id": 100,
                "wecom_secret": "secret",
                "wecom_admin_whitelist": "alice,bob",
                "notify_registration_wecom": True,
                "notify_expiry_wecom": False,
            }
        )
        try:
            with patch.object(notify, "_send_wecom", new=AsyncMock()) as sender:
                await notify.push("注册", "正文", category="registration")
                sender.assert_awaited_once()
                config_call = sender.await_args
                self.assertEqual(config_call.args, ("注册", "正文"))
            with patch.object(notify, "_send_wecom", new=AsyncMock()) as sender:
                await notify.push("到期", "正文", category="expiry")
                sender.assert_not_awaited()
        finally:
            settings_store._cache = original

    def test_menu_validator_rejects_official_shape_violations(self):
        valid = {"button": [{"name": "入口", "sub_button": [{"type": "click", "name": "查询", "key": "query"}]}]}
        validate_menu(valid)
        validate_menu({"button": [{"name": "入口", "type": "view_limited", "media_id": "media-1"}]})
        with self.assertRaises(Exception):
            validate_menu({"button": [{"name": "入口", "type": "view_limited", "url": "https://example.test"}]})
        cases = [
            {"button": []},
            {"button": [{"name": "入口", "sub_button": []}]},
            {"button": [{"name": "入口", "sub_button": [{"type": "click", "name": "查询"}]}]},
            {"button": [{"name": "入口", "sub_button": [{"type": "click", "name": "查询", "key": "x" * 129}]}]},
            {"button": [{"name": "入口", "sub_button": [{"type": "unknown", "name": "查询", "key": "x"}]}]},
            {"button": [{"name": "入口", "sub_button": [{"type": "click", "name": "查询", "key": "x"}], "type": "click"}]},
        ]
        for menu in cases:
            with self.assertRaises(Exception):
                validate_menu(menu)

    async def test_notification_channel_failures_are_isolated_and_redacted(self):
        original = settings_store.current()
        settings_store._cache = original.__class__(
            **{
                **original.__dict__,
                "telegram_bot_token": "raw-token",
                "telegram_chat_id": "chat",
                "notify_webhook_url": "https://example.test/hook?secret=raw-secret",
                "wecom_corp_id": "wwcorp",
                "wecom_agent_id": 100,
                "wecom_secret": "raw-secret",
                "notify_registration_telegram": True,
                "notify_registration_webhook": True,
                "notify_registration_wecom": True,
            }
        )
        try:
            with patch.object(
                notify, "_send_telegram", new=AsyncMock(side_effect=RuntimeError("raw-token"))
            ) as telegram, patch.object(notify, "_send_webhook", new=AsyncMock()) as webhook, patch.object(
                notify, "_send_wecom", new=AsyncMock()
            ) as wecom, self.assertLogs("app.notify", level="WARNING") as captured:
                await notify.push("注册", "正文", category="registration")
            telegram.assert_awaited_once()
            webhook.assert_awaited_once()
            wecom.assert_awaited_once()
            joined = "\n".join(captured.output)
            self.assertNotIn("raw-token", joined)
            self.assertNotIn("raw-secret", joined)
        finally:
            settings_store._cache = original

    async def test_notification_defaults_are_off_and_require_complete_config(self):
        original = settings_store.current()
        defaults = settings_store._from_env()
        self.assertFalse(defaults.notify_registration_webhook)
        self.assertFalse(defaults.notify_registration_telegram)
        self.assertFalse(defaults.notify_registration_wecom)
        settings_store._cache = original.__class__(
            **{
                **original.__dict__,
                "notify_registration_webhook": True,
                "notify_registration_telegram": True,
                "notify_registration_wecom": True,
            }
        )
        try:
            with patch.object(notify, "_send_telegram", new=AsyncMock()) as telegram, patch.object(
                notify, "_send_webhook", new=AsyncMock()
            ) as webhook, patch.object(notify, "_send_wecom", new=AsyncMock()) as wecom:
                await notify.push("注册", "正文", category="registration")
            telegram.assert_not_awaited()
            webhook.assert_not_awaited()
            wecom.assert_not_awaited()

            settings_store._cache = original.__class__(
                **{
                    **settings_store.current().__dict__,
                    "telegram_bot_token": "token",
                    "telegram_chat_id": "chat",
                    "notify_registration_telegram": False,
                }
            )
            with patch.object(notify, "_send_telegram", new=AsyncMock()) as telegram:
                await notify.push("注册", "正文", category="registration")
            telegram.assert_not_awaited()

            settings_store._cache = original.__class__(
                **{
                    **settings_store.current().__dict__,
                    "notify_registration_telegram": True,
                }
            )
            with patch.object(notify, "_send_telegram", new=AsyncMock()) as telegram:
                await notify.push("注册", "正文", category="registration")
            telegram.assert_awaited_once()
        finally:
            settings_store._cache = original

    async def test_registration_and_expiry_switches_cover_all_channels(self):
        original = settings_store.current()
        settings_store._cache = original.__class__(
            **{
                **original.__dict__,
                "telegram_bot_token": "token",
                "telegram_chat_id": "chat",
                "notify_webhook_url": "https://example.test/hook",
                "wecom_corp_id": "wwcorp",
                "wecom_agent_id": 100,
                "wecom_secret": "secret",
                "notify_registration_telegram": True,
                "notify_registration_webhook": False,
                "notify_registration_wecom": True,
                "notify_expiry_telegram": False,
                "notify_expiry_webhook": True,
                "notify_expiry_wecom": False,
            }
        )
        try:
            with patch.object(notify, "_send_telegram", new=AsyncMock()) as telegram, patch.object(
                notify, "_send_webhook", new=AsyncMock()
            ) as webhook, patch.object(notify, "_send_wecom", new=AsyncMock()) as wecom:
                await notify.push("注册", "正文", category="registration")
                telegram.assert_awaited_once()
                webhook.assert_not_awaited()
                wecom.assert_awaited_once()

            with patch.object(notify, "_send_telegram", new=AsyncMock()) as telegram, patch.object(
                notify, "_send_webhook", new=AsyncMock()
            ) as webhook, patch.object(notify, "_send_wecom", new=AsyncMock()) as wecom:
                await notify.push("到期", "正文", category="expiry")
                telegram.assert_not_awaited()
                webhook.assert_awaited_once()
                wecom.assert_not_awaited()
        finally:
            settings_store._cache = original

    async def test_bill_activation_notification_contains_fields_and_is_idempotent(self):
        original = settings_store.current()
        settings_store._cache = original.__class__(
            **{
                **original.__dict__,
                "wecom_corp_id": "wwcorp",
                "wecom_agent_id": 100,
                "wecom_secret": "secret",
                "notify_activation_webhook": False,
                "notify_activation_telegram": False,
                "notify_activation_wecom": True,
            }
        )
        notify._activation_notified_bill_codes.clear()
        event = {
            "username": "alice",
            "activated_at": "2026-09-06T08:30:45+08:00",
            "bill_code": "100012345620260906999999999999",
            "redeem_code": "REDEEM-SECRET-123",
        }
        try:
            with patch.object(notify, "_send_wecom", new=AsyncMock()) as sender:
                await notify.activation_success(event)
                await notify.activation_success(event)
            sender.assert_awaited_once()
            self.assertEqual(sender.await_args.args[0], "账单码激活成功")
            message = sender.await_args.args[1]
            self.assertIn("用户名：alice", message)
            self.assertIn("激活时间：2026-09-06 08:30:45", message)
            self.assertIn(event["bill_code"], message)
            self.assertIn("已生成 30 天兑换码", message)
            self.assertNotIn(event["redeem_code"], message)
        finally:
            notify._activation_notified_bill_codes.clear()
            settings_store._cache = original

    async def test_bill_activation_notification_switch_and_failure_retry(self):
        original = settings_store.current()
        event = {
            "username": "alice",
            "activated_at": "2026-09-06T08:30:45+08:00",
            "bill_code": "100012345620260906888888888888",
            "redeem_code": "REDEEM-SECRET-456",
        }
        settings_store._cache = original.__class__(
            **{
                **original.__dict__,
                "wecom_corp_id": "wwcorp",
                "wecom_agent_id": 100,
                "wecom_secret": "secret",
                "notify_activation_webhook": False,
                "notify_activation_telegram": False,
                "notify_activation_wecom": False,
            }
        )
        notify._activation_notified_bill_codes.clear()
        try:
            with patch.object(notify, "_send_wecom", new=AsyncMock()) as sender:
                await notify.activation_success(event)
            sender.assert_not_awaited()

            settings_store._cache = original.__class__(
                **{
                    **settings_store.current().__dict__,
                    "notify_activation_wecom": True,
                }
            )
            sender = AsyncMock(side_effect=[RuntimeError("provider failure"), None])
            with patch.object(notify, "_send_wecom", new=sender):
                await notify.activation_success(event)
                await notify.activation_success(event)
            self.assertEqual(sender.await_count, 2)
        finally:
            notify._activation_notified_bill_codes.clear()
            settings_store._cache = original

    async def test_bill_activation_wecom_failure_is_not_hidden_by_other_channel(self):
        original = settings_store.current()
        settings_store._cache = original.__class__(
            **{
                **original.__dict__,
                "wecom_corp_id": "wwcorp",
                "wecom_agent_id": 100,
                "wecom_secret": "secret",
                "telegram_bot_token": "telegram-token",
                "telegram_chat_id": "chat",
                "notify_activation_telegram": True,
                "notify_activation_webhook": False,
                "notify_activation_wecom": True,
            }
        )
        notify._activation_notified_bill_codes.clear()
        event = {
            "username": "alice",
            "activated_at": "2026-09-06T08:30:45+08:00",
            "bill_code": "100012345620260906666666666666",
            "redeem_code": "REDEEM-SECRET-999",
        }
        try:
            with patch.object(notify, "_send_telegram", new=AsyncMock()) as telegram, patch.object(
                notify, "_send_wecom", new=AsyncMock(side_effect=RuntimeError("failed"))
            ) as wecom:
                result = await notify.activation_success(event)
            self.assertFalse(result)
            telegram.assert_awaited_once()
            wecom.assert_awaited_once()
            self.assertNotIn(event["bill_code"], notify._activation_notified_bill_codes)
        finally:
            notify._activation_notified_bill_codes.clear()
            settings_store._cache = original

    async def test_bill_activation_notification_redacts_codes_from_logs(self):
        original = settings_store.current()
        settings_store._cache = original.__class__(
            **{
                **original.__dict__,
                "wecom_corp_id": "wwcorp",
                "wecom_agent_id": 100,
                "wecom_secret": "secret",
                "notify_activation_webhook": False,
                "notify_activation_telegram": False,
                "notify_activation_wecom": True,
            }
        )
        bill_code = "100012345620260906777777777777"
        redeem_code = "REDEEM-SECRET-789"
        notify._activation_notified_bill_codes.clear()
        try:
            with patch.object(notify, "_send_wecom", new=AsyncMock()), self.assertLogs(
                "app.notify", level="INFO"
            ) as captured:
                await notify.activation_success(
                    {
                        "username": "alice",
                        "activated_at": "2026-09-06T08:30:45+08:00",
                        "bill_code": bill_code,
                        "redeem_code": redeem_code,
                    }
                )
            joined = "\n".join(captured.output)
            self.assertNotIn(bill_code, joined)
            self.assertNotIn(redeem_code, joined)
            self.assertNotIn("secret", joined)
        finally:
            notify._activation_notified_bill_codes.clear()
            settings_store._cache = original

    async def test_info_notification_log_contains_message_without_credentials(self):
        original = settings_store.current()
        settings_store._cache = original.__class__(
            **{
                **original.__dict__,
                "telegram_bot_token": "raw-telegram-token",
                "telegram_chat_id": "chat",
                "notify_registration_telegram": True,
                "notify_registration_webhook": False,
                "notify_registration_wecom": False,
            }
        )
        try:
            with patch.object(notify, "_send_telegram", new=AsyncMock()) as sender, self.assertLogs(
                "app.notify", level="INFO"
            ) as captured:
                await notify.push("测试标题", "测试消息 raw-telegram-token", category="registration")
            sender.assert_awaited_once()
            joined = "\n".join(captured.output)
            self.assertIn("通知发送成功", joined)
            self.assertIn("测试消息", joined)
            self.assertNotIn("raw-telegram-token", joined)
        finally:
            settings_store._cache = original

    def test_encrypted_reply_and_message_parser(self):
        key = bytes(range(32))
        aes_key = base64.b64encode(key).decode("ascii").rstrip("=")
        reply = encrypted_text_reply(
            "token",
            aes_key,
            content="内容 ]]> 校验",
            from_user="alice",
            to_user="wwcorp",
            receive_id="wwcorp",
            timestamp="123",
            nonce="nonce",
        )
        root = ET.fromstring(reply)
        encrypted = root.findtext("Encrypt")
        self.assertTrue(encrypted)
        self.assertEqual(
            decrypt_callback(aes_key, encrypted)[1],
            "wwcorp",
        )
        plaintext, _ = decrypt_callback(aes_key, encrypted)
        fields = parse_message_xml(plaintext)
        self.assertEqual(fields["ToUserName"], "alice")
        self.assertEqual(fields["FromUserName"], "wwcorp")
        self.assertEqual(fields["Content"], "内容 ]]> 校验")


if __name__ == "__main__":
    unittest.main()
