import base64
import time
import unittest
from urllib.parse import urlencode
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from app import routes, settings_store, wecom, wecom_commands
from app.main import app


class WeComCallbackTests(unittest.IsolatedAsyncioTestCase):
    token = "callback-token"
    corp_id = "ww-test-corp"
    agent_id = 1000002

    @classmethod
    def setUpClass(cls):
        key = bytes(range(32))
        cls.aes_key = base64.b64encode(key).decode("ascii").rstrip("=")
        cls.runtime = settings_store.current().__class__(
            **{
                **settings_store.current().__dict__,
                "wecom_corp_id": cls.corp_id,
                "wecom_agent_id": cls.agent_id,
                "wecom_token": cls.token,
                "wecom_encoding_aes_key": cls.aes_key,
                "wecom_admin_whitelist": "alice",
            }
        )

    def _request(self, method: str, query: str = "", body: bytes = b"", content_type: str = "application/xml") -> Request:
        consumed = False

        async def receive():
            nonlocal consumed
            if consumed:
                return {"type": "http.disconnect"}
            consumed = True
            return {"type": "http.request", "body": body, "more_body": False}

        raw_path = b"/wechat" + (b"?" + query.encode("ascii") if query else b"")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": "/wechat",
            "raw_path": raw_path,
            "query_string": query.encode("ascii"),
            "headers": [(b"content-type", content_type.encode("ascii"))],
            "client": ("198.51.100.10", 443),
            "server": ("example.test", 443),
        }
        return Request(scope, receive)

    def _query(
        self,
        encrypted: str,
        *,
        timestamp: str | None = None,
        nonce: str = "nonce",
        include_echostr: bool = False,
    ) -> str:
        timestamp = timestamp or str(int(time.time()))
        values = {
            "msg_signature": wecom.callback_signature(self.token, timestamp, nonce, encrypted),
            "timestamp": timestamp,
            "nonce": nonce,
        }
        if include_echostr:
            values["echostr"] = encrypted
        return urlencode(values)

    def _outer(self, encrypted: str, *, receive_id: str = "ww-test-corp", agent_id: str = "") -> bytes:
        agent = f"<AgentID>{agent_id}</AgentID>" if agent_id else ""
        return (
            "<xml>"
            f"<ToUserName><![CDATA[{receive_id}]]></ToUserName>"
            f"{agent}"
            f"<Encrypt><![CDATA[{encrypted}]]></Encrypt>"
            "</xml>"
        ).encode("utf-8")

    def _message(
        self,
        *,
        agent_id: int = agent_id,
        content: str = "hello",
        receive_id: str = corp_id,
        from_user: str = "alice",
        msg_type: str = "text",
        event: str = "",
        event_key: str = "",
    ) -> str:
        event_xml = f"<Event><![CDATA[{event}]]></Event><EventKey><![CDATA[{event_key}]]></EventKey>" if event else ""
        return (
            "<xml>"
            f"<ToUserName><![CDATA[{receive_id}]]></ToUserName>"
            f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
            f"<CreateTime>{int(time.time())}</CreateTime>"
            f"<MsgType><![CDATA[{msg_type}]]></MsgType>"
            f"<Content><![CDATA[{content}]]></Content>"
            f"{event_xml}"
            f"<AgentID>{agent_id}</AgentID>"
            "</xml>"
        )

    def test_only_canonical_wechat_route_is_registered(self):
        callback_routes = {
            (route.path, tuple(sorted(route.methods or ())))
            for route in app.routes
            if getattr(route, "path", "") in {"/wechat", "/wecom/callback"}
        }
        self.assertEqual(callback_routes, {("/wechat", ("GET",)), ("/wechat", ("POST",))})

    async def test_get_verification_returns_decrypted_plaintext(self):
        encrypted = wecom.encrypt_callback(self.aes_key, "echo-value", self.corp_id)
        query = self._query(encrypted, include_echostr=True)
        with patch.object(settings_store, "current", return_value=self.runtime):
            response = await routes.wecom_callback_verify(self._request("GET", query=query))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "text/plain")
        self.assertEqual(response.body.decode("utf-8"), "echo-value")
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))

    async def test_get_rejects_bad_signature_and_stale_timestamp_without_secret_echo(self):
        encrypted = wecom.encrypt_callback(self.aes_key, "echo-value", self.corp_id)
        timestamp = str(int(time.time()) - 601)
        query = urlencode(
            {
                "msg_signature": "not-a-valid-signature",
                "timestamp": timestamp,
                "nonce": "nonce",
                "echostr": encrypted,
            }
        )
        with patch.object(settings_store, "current", return_value=self.runtime):
            response = await routes.wecom_callback_verify(self._request("GET", query=query))
        self.assertEqual(response.status_code, 403)
        body = response.body.decode("utf-8")
        self.assertNotIn(self.token, body)
        self.assertNotIn(self.aes_key, body)

        wrong_corp_encrypted = wecom.encrypt_callback(self.aes_key, "echo-value", "wrong-corp")
        wrong_corp_query = self._query(wrong_corp_encrypted, include_echostr=True)
        with patch.object(settings_store, "current", return_value=self.runtime):
            wrong_corp_response = await routes.wecom_callback_verify(
                self._request("GET", query=wrong_corp_query)
            )
        self.assertEqual(wrong_corp_response.status_code, 403)
        self.assertIn("CorpID", wrong_corp_response.body.decode("utf-8"))

    async def test_post_validates_query_signature_and_decrypted_agent_id(self):
        encrypted = wecom.encrypt_callback(self.aes_key, self._message(content="状态"), self.corp_id)
        query = self._query(encrypted)
        with patch.object(settings_store, "current", return_value=self.runtime), patch.object(
            wecom_commands, "execute_quick", new=AsyncMock(return_value="状态正常")
        ) as execute:
            response = await routes.wecom_callback_receive(
                self._request("POST", query=query, body=self._outer(encrypted)), None
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "application/xml")
        execute.assert_awaited_once_with(None, wecom_commands.CommandRequest("status"))
        reply_root = wecom.ET.fromstring(response.body)
        reply_encrypted = reply_root.findtext("Encrypt")
        self.assertTrue(reply_encrypted)
        reply_plaintext, receive_id = wecom.decrypt_callback(self.aes_key, reply_encrypted)
        self.assertEqual(receive_id, self.corp_id)
        self.assertEqual(wecom.parse_message_xml(reply_plaintext)["Content"], "状态正常")

    async def test_post_rejects_json_missing_query_and_wrong_agent_without_secret_echo(self):
        encrypted = wecom.encrypt_callback(self.aes_key, self._message(), self.corp_id)
        with patch.object(settings_store, "current", return_value=self.runtime):
            json_response = await routes.wecom_callback_receive(
                self._request("POST", query=self._query(encrypted), body=b'{"Encrypt":"x"}', content_type="application/json"),
                None,
            )
            missing_query = await routes.wecom_callback_receive(
                self._request("POST", body=self._outer(encrypted)), None
            )
            wrong_encrypted = wecom.encrypt_callback(
                self.aes_key, self._message(agent_id=self.agent_id + 1), self.corp_id
            )
            wrong_agent = await routes.wecom_callback_receive(
                self._request("POST", query=self._query(wrong_encrypted), body=self._outer(wrong_encrypted)),
                None,
            )
        for response in (json_response, missing_query, wrong_agent):
            self.assertEqual(response.status_code, 403)
            body = response.body.decode("utf-8")
            self.assertNotIn(self.token, body)
            self.assertNotIn(self.aes_key, body)
        self.assertIn("XML", json_response.body.decode("utf-8"))
        self.assertIn("参数", missing_query.body.decode("utf-8"))
        self.assertIn("AgentID", wrong_agent.body.decode("utf-8"))

    async def test_unknown_menu_event_returns_success_without_executing(self):
        encrypted = wecom.encrypt_callback(
            self.aes_key,
            self._message(msg_type="event", event="click", event_key="unknown_event"),
            self.corp_id,
        )
        with patch.object(settings_store, "current", return_value=self.runtime), patch.object(
            wecom_commands, "execute_quick", new=AsyncMock()
        ) as execute:
            response = await routes.wecom_callback_receive(
                self._request("POST", query=self._query(encrypted), body=self._outer(encrypted)), None
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"success")
        execute.assert_not_awaited()

    async def test_code_delete_menu_starts_interactive_numbered_flow(self):
        encrypted = wecom.encrypt_callback(
            self.aes_key,
            self._message(msg_type="event", event="click", event_key="code_delete"),
            self.corp_id,
        )
        with patch.object(settings_store, "current", return_value=self.runtime), patch.object(
            wecom_commands, "begin_code_delete", new=AsyncMock(return_value="1、CODE-ONE（30 天）")
        ) as begin:
            response = await routes.wecom_callback_receive(
                self._request("POST", query=self._query(encrypted), body=self._outer(encrypted)), None
            )
        self.assertEqual(response.status_code, 200)
        begin.assert_awaited_once_with(None, "alice")
        reply_encrypted = wecom.ET.fromstring(response.body).findtext("Encrypt")
        reply_plaintext, _ = wecom.decrypt_callback(self.aes_key, reply_encrypted)
        self.assertEqual(wecom.parse_message_xml(reply_plaintext)["Content"], "1、CODE-ONE（30 天）")

    async def test_interactive_command_args_are_not_written_to_logs(self):
        encrypted = wecom.encrypt_callback(
            self.aes_key,
            self._message(msg_type="text", content="pw-secret"),
            self.corp_id,
        )
        request = wecom_commands.CommandRequest(
            "add_account", ("主服务器/newuser", "pw-secret", "播放", "到期天数=30")
        )
        with patch.object(settings_store, "current", return_value=self.runtime), patch.object(
            wecom_commands, "consume_interaction", new=AsyncMock(return_value=request)
        ) as consume, patch.object(
            wecom_commands, "run_background", new=AsyncMock()
        ), self.assertLogs("app.routes", level="INFO") as captured:
            response = await routes.wecom_callback_receive(
                self._request("POST", query=self._query(encrypted), body=self._outer(encrypted)), None
            )
        self.assertEqual(response.body, b"success")
        self.assertIn("command=add_account", "\n".join(captured.output))
        self.assertNotIn("pw-secret", "\n".join(captured.output))
        consume.assert_awaited_once()

    async def test_non_whitelist_menu_event_returns_success_without_executing(self):
        encrypted = wecom.encrypt_callback(
            self.aes_key,
            self._message(from_user="bob", msg_type="event", event="click", event_key="user_add"),
            self.corp_id,
        )
        with patch.object(settings_store, "current", return_value=self.runtime), patch.object(
            wecom_commands, "execute_quick", new=AsyncMock()
        ) as execute:
            response = await routes.wecom_callback_receive(
                self._request("POST", query=self._query(encrypted), body=self._outer(encrypted)), None
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"success")
        execute.assert_not_awaited()

    def test_callback_parsers_reject_json_and_non_xml_root(self):
        for body in (b"{\"Encrypt\":\"x\"}", b"<message><Encrypt>x</Encrypt></message>"):
            with self.assertRaises(wecom.WeComError):
                wecom.parse_callback_xml(body)

    def test_callback_log_text_redaction_does_not_retain_credentials(self):
        raw = (
            f"token={self.token} EncodingAESKey={self.aes_key} "
            "https://user:password@example.test/callback?corpsecret=secret-value"
        )
        redacted = routes._redact_sensitive_text(raw)
        self.assertNotIn(self.token, redacted)
        self.assertNotIn(self.aes_key, redacted)
        self.assertNotIn("password@example.test", redacted)
        self.assertNotIn("secret-value", redacted)
        self.assertIn("[REDACTED]", redacted)


if __name__ == "__main__":
    unittest.main()
