import logging
import unittest

import httpx

from app.logging_config import configure_logging, register_sensitive_values


class LoggingSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configure_logging()

    def test_http_client_records_redact_channel_credentials_and_query(self):
        corp = "FAKE_CORP"
        secret = "FAKE_SECRET"
        access = "FAKE_ACCESS"
        telegram = "123456:FAKE_TELEGRAM_TOKEN"
        webhook = "https://hooks.example.test/callback?secret=FAKE_SECRET&access_token=FAKE_ACCESS"
        register_sensitive_values(corp, secret, access, telegram, webhook)

        with self.assertLogs("httpx", level="INFO") as captured:
            logging.getLogger("httpx").info(
                "HTTP Request: GET https://qyapi.weixin.qq.com/cgi-bin/gettoken?"
                "corpid=%s&corpsecret=%s Authorization: Bearer %s "
                "headers={'X-Emby-Token': '%s'} body={'token': '%s'} HTTP/1.1 200 OK",
                corp,
                secret,
                access,
                access,
                telegram,
            )
            logging.getLogger("httpx").warning(
                "secret_key=raw-key password_hash=raw-hash api_key_encrypted=raw-api "
                "X-SECRET-KEY: raw-prefix Cookie: raw-cookie"
            )
            logging.getLogger("httpcore").info(
                "HTTP Request: POST %s Authorization: bearer %s HTTP/1.1 200 OK",
                webhook,
                telegram,
            )

        joined = "\n".join(captured.output)
        for value in (
            corp,
            secret,
            access,
            telegram,
            "FAKE_TELEGRAM_TOKEN",
            "raw-key",
            "raw-hash",
            "raw-api",
            "raw-prefix",
            "raw-cookie",
        ):
            self.assertNotIn(value, joined)
        self.assertNotIn("secret=FAKE_SECRET", joined)
        self.assertIn("qyapi.weixin.qq.com/cgi-bin/gettoken", joined)
        self.assertIn("HTTP/1.1 200 OK", joined)
        self.assertNotIn("headers=", joined)
        self.assertNotIn("body=", joined)
        self.assertIn("[REDACTED]", joined)

    def test_exception_records_keep_type_without_raw_text(self):
        register_sensitive_values("FAKE_SECRET", "FAKE_ACCESS", "raw-body-token")
        with self.assertLogs("httpcore", level="WARNING") as captured:
            try:
                raise RuntimeError(
                    "connect https://example.test/?corpsecret=FAKE_SECRET "
                    "Authorization: Bearer FAKE_ACCESS body=raw-body-token"
                )
            except RuntimeError:
                logging.getLogger("httpcore").exception("request failed")

        joined = "\n".join(captured.output)
        for value in ("FAKE_SECRET", "FAKE_ACCESS", "raw-body-token"):
            self.assertNotIn(value, joined)
        self.assertIn("error_type=RuntimeError", joined)

    def test_real_httpx_mock_transport_does_not_log_request_credentials(self):
        async def exercise():
            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, request=request, json={"ok": True})

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                await client.get(
                    "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                    params={"corpid": "FAKE_CORP", "corpsecret": "FAKE_SECRET"},
                )
                await client.post(
                    "https://hooks.example.test/callback?secret=FAKE_SECRET",
                    headers={"Authorization": "Bearer FAKE_ACCESS"},
                    json={"token": "123456:FAKE_TELEGRAM_TOKEN"},
                )

        register_sensitive_values(
            "FAKE_CORP",
            "FAKE_SECRET",
            "FAKE_ACCESS",
            "123456:FAKE_TELEGRAM_TOKEN",
        )
        with self.assertLogs("httpx", level="INFO") as captured:
            import asyncio

            asyncio.run(exercise())
        joined = "\n".join(captured.output)
        for value in (
            "FAKE_CORP",
            "FAKE_SECRET",
            "FAKE_ACCESS",
            "123456:FAKE_TELEGRAM_TOKEN",
        ):
            self.assertNotIn(value, joined)
        self.assertIn("HTTP Request", joined)
        self.assertIn("HTTP/1.1 200 OK", joined)


if __name__ == "__main__":
    unittest.main()
