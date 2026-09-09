import asyncio
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path


_temp_dir = tempfile.TemporaryDirectory(prefix="emby-apex-dashboard-api-")
os.environ["APEX_DATA"] = str(Path(_temp_dir.name) / "data")
os.environ["APEX_IMAGE"] = str(Path(_temp_dir.name) / "image")

from fastapi.testclient import TestClient  # noqa: E402

from app import scheduler  # noqa: E402
from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ActionLog, Base, ManagedUser, PlaybackRecord, Server, utcnow  # noqa: E402


@asynccontextmanager
async def _test_lifespan(_app):
    yield


class DashboardApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())
        cls.original_lifespan = app.router.lifespan_context
        app.router.lifespan_context = _test_lifespan

    @classmethod
    def tearDownClass(cls):
        app.router.lifespan_context = cls.original_lifespan
        scheduler.live_cache = []

    def setUp(self):
        asyncio.run(self._reset_data())
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    async def _reset_data(self):
        async with engine.begin() as conn:
            for table in reversed(list(Base.metadata.sorted_tables)):
                await conn.execute(table.delete())
        scheduler.live_cache = []

    def _login(self):
        token = self.client.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
        response = self.client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "admin", "csrf_token": token},
        )
        self.assertEqual(response.status_code, 200)

    def test_requires_admin_session_and_common_error_envelope(self):
        response = self.client.get("/api/v1/dashboard")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            set(response.json()),
            {"ok", "data", "error"},
        )
        self.assertFalse(response.json()["ok"])
        self.assertIsNone(response.json()["data"])
        self.assertTrue(response.json()["error"])

    def test_empty_data_shape_and_zero_filled_trend(self):
        self._login()
        response = self.client.get("/api/v1/dashboard")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"ok", "data", "error"})
        self.assertTrue(body["ok"])
        self.assertIsNone(body["error"])
        data = body["data"]
        self.assertEqual(
            set(data), {"summary", "sessions", "watch_time", "trend", "servers", "logs", "poll_interval"}
        )
        self.assertEqual(data["summary"], {"servers": 0, "users": 0, "disabled": 0, "expiring": 0, "playing": 0})
        self.assertEqual(data["sessions"], [])
        self.assertEqual(data["servers"], [])
        self.assertEqual(data["logs"], [])
        self.assertEqual(data["watch_time"]["hours"], 0)
        self.assertEqual(data["watch_time"]["users"], [])
        self.assertEqual(len(data["trend"]), 7)
        self.assertTrue(all(point["plays"] == 0 and point["hours"] == 0 for point in data["trend"]))
        self.assertIsInstance(data["poll_interval"], int)

    def test_logs_filter_and_ten_item_pagination(self):
        async def seed():
            async with SessionLocal() as db:
                for index in range(13):
                    db.add(
                        ActionLog(
                            action=f"log-{index}",
                            level="error" if index % 2 else "info",
                            detail=f"detail-{index}",
                            created_at=utcnow() - timedelta(seconds=index),
                        )
                    )
                await db.commit()

        asyncio.run(seed())
        self._login()
        first = self.client.get("/api/v1/logs?level=info&page=1")
        self.assertEqual(first.status_code, 200)
        first_data = first.json()["data"]
        self.assertEqual(first_data["page_size"], 10)
        self.assertLessEqual(len(first_data["logs"]), 10)
        self.assertTrue(all(row["level"] == "info" for row in first_data["logs"]))
        self.assertEqual(first_data["total"], 7)
        invalid = self.client.get("/api/v1/logs?level=trace")
        self.assertEqual(invalid.status_code, 400)

    def test_dashboard_today_watch_time_excludes_open_and_old_records(self):
        async def seed():
            async with SessionLocal() as db:
                server = Server(name="watch-server", base_url="http://watch", api_key_encrypted="x")
                db.add(server)
                await db.flush()
                now = utcnow()
                db.add_all(
                    [
                        PlaybackRecord(
                            server_id=server.id,
                            session_key="watch-ended",
                            emby_user_id="u1",
                            username="alice",
                            watched_seconds=7200,
                            started_at=now - timedelta(hours=1),
                            last_seen_at=now - timedelta(minutes=5),
                            ended_at=now - timedelta(minutes=5),
                        ),
                        PlaybackRecord(
                            server_id=server.id,
                            session_key="watch-open",
                            emby_user_id="u2",
                            username="bob",
                            watched_seconds=3600,
                            started_at=now - timedelta(minutes=30),
                            last_seen_at=now,
                            ended_at=None,
                        ),
                        PlaybackRecord(
                            server_id=server.id,
                            session_key="watch-old",
                            emby_user_id="u3",
                            username="carol",
                            watched_seconds=1800,
                            started_at=now - timedelta(days=2),
                            last_seen_at=now - timedelta(days=2),
                            ended_at=now - timedelta(days=2),
                        ),
                    ]
                )
                await db.commit()

        asyncio.run(seed())
        self._login()
        response = self.client.get("/api/v1/dashboard")
        self.assertEqual(response.status_code, 200)
        watch_time = response.json()["data"]["watch_time"]
        self.assertEqual(watch_time["hours"], 2.0)
        self.assertEqual(watch_time["users"], [{"username": "alice", "plays": 1, "hours": 2.0}])

    def test_authenticated_payload_is_limited_and_redacted(self):
        async def seed():
            async with SessionLocal() as db:
                server = Server(
                    name="secure-server",
                    base_url="https://user:pass@example.test:8920/emby?api_key=raw-key",
                    api_key_encrypted="cipher-secret",
                    enabled=True,
                    server_version="4.8.0",
                    last_error="api_key=raw-key",
                    last_ok_at=utcnow(),
                )
                disabled = ManagedUser(
                    server_id=1,
                    emby_user_id="disabled-user",
                    username="disabled-user",
                    is_disabled=True,
                )
                db.add(server)
                await db.flush()
                disabled.server_id = server.id
                db.add(disabled)
                for index in range(20):
                    db.add(
                        ActionLog(
                            action="server_updated" if index == 0 else f"action-{index}",
                            detail=(
                                "TMDB request https://emby.example/api?api_key=raw-key"
                                "&token=secret-token&secret=secret-value"
                                if index == 0
                                else f"detail-{index}"
                            ),
                            created_at=utcnow() - timedelta(seconds=index),
                        )
                    )
                await db.commit()

        asyncio.run(seed())
        self._login()
        response = self.client.get("/api/v1/dashboard")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["summary"]["servers"], 1)
        self.assertEqual(data["summary"]["users"], 1)
        self.assertEqual(data["summary"]["disabled"], 1)
        self.assertEqual(len(data["servers"]), 1)
        server = data["servers"][0]
        self.assertEqual(server["base_url"], "https://example.test:8920")
        self.assertEqual(server["status"], "error")
        self.assertTrue(server["has_error"])
        self.assertNotIn("api_key_encrypted", server)
        self.assertNotIn("last_error", server)
        self.assertNotIn("raw-key", response.text)
        self.assertNotIn("secret-token", response.text)
        self.assertNotIn("secret-value", response.text)
        self.assertNotIn("cipher-secret", response.text)
        self.assertNotIn("secret_key", response.text)
        self.assertIn("[REDACTED]", data["logs"][0]["detail"])
        self.assertEqual(len(data["logs"]), 1)
        self.assertEqual(data["logs"][0]["action"], "server_updated")

    def test_credential_key_variants_are_redacted_in_response_logs(self):
        cases = [
            ("secret_key=raw-secret-key", "secret_key=[REDACTED]", "raw-secret-key"),
            ("password_hash: raw-password-hash", "password_hash: [REDACTED]", "raw-password-hash"),
            (
                '"api_key_encrypted": "raw-api-key-encrypted"',
                '"api_key_encrypted": "[REDACTED]"',
                "raw-api-key-encrypted",
            ),
            ("X-SECRET-KEY = raw-prefix-secret", "X-SECRET-KEY = [REDACTED]", "raw-prefix-secret"),
            (
                "Authorization: Bearer raw-bearer-token",
                "Authorization: Bearer [REDACTED]",
                "raw-bearer-token",
            ),
            (
                "AUTHORIZATION = bearer raw-bearer-equals",
                "AUTHORIZATION = bearer [REDACTED]",
                "raw-bearer-equals",
            ),
            (
                "https://example.test/path?API-KEY-ENCRYPTED=raw-url-api&secretKey=raw-url-secret",
                "https://example.test/path?API-KEY-ENCRYPTED=[REDACTED]&secretKey=[REDACTED]",
                "raw-url-api",
            ),
            ("client-secret: raw-client-secret", "client-secret: [REDACTED]", "raw-client-secret"),
            (
                "prefix_password_suffix=raw-password-suffix",
                "prefix_password_suffix=[REDACTED]",
                "raw-password-suffix",
            ),
            (
                "context: secret_key=raw-nested-secret",
                "context: secret_key=[REDACTED]",
                "raw-nested-secret",
            ),
            (
                "headers: Authorization: Bearer raw-nested-token",
                "headers: Authorization: Bearer [REDACTED]",
                "raw-nested-token",
            ),
            (
                "api_key_encrypted raw-space-api-key",
                "api_key_encrypted [REDACTED]",
                "raw-space-api-key",
            ),
        ]

        async def seed():
            async with SessionLocal() as db:
                for index, (detail, _, _) in enumerate(cases):
                    db.add(
                        ActionLog(
                            action=f"variant-{index}",
                            detail=detail,
                            created_at=utcnow() - timedelta(seconds=index),
                        )
                    )
                await db.commit()

        asyncio.run(seed())
        self._login()
        response = self.client.get("/api/v1/logs?page=1")
        response2 = self.client.get("/api/v1/logs?page=2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response2.status_code, 200)
        self.assertEqual(response.json()["ok"], True)
        response_text = response.text + response2.text
        details = "\n".join(
            item["detail"]
            for payload in (response.json(), response2.json())
            for item in payload["data"]["logs"]
        )
        for _, expected, secret in cases:
            self.assertIn(expected, details)
            self.assertNotIn(secret, response_text)
        self.assertNotIn("raw-url-secret", response_text)

    def test_snapshot_exception_uses_safe_500_envelope(self):
        from app import routes as admin_routes

        self._login()
        original_snapshot = admin_routes._dashboard_snapshot

        async def fail_snapshot(_db):
            raise RuntimeError("raw-key secret-token")

        admin_routes._dashboard_snapshot = fail_snapshot
        try:
            with self.assertLogs("app.api", level="ERROR") as captured:
                response = self.client.get("/api/v1/dashboard")
        finally:
            admin_routes._dashboard_snapshot = original_snapshot

        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertEqual(set(body), {"ok", "data", "error"})
        self.assertFalse(body["ok"])
        self.assertIsNone(body["data"])
        self.assertEqual(body["error"], "总览数据暂时不可用，请稍后重试")
        self.assertNotIn("raw-key", response.text)
        self.assertNotIn("secret-token", response.text)
        self.assertFalse(any("raw-key" in message for message in captured.output))
        self.assertTrue(any("error_type=RuntimeError" in message for message in captured.output))

    def test_serialization_exception_uses_safe_500_envelope(self):
        from app import routes as admin_routes

        self._login()
        original_snapshot = admin_routes._dashboard_snapshot

        async def invalid_snapshot(_db):
            return {
                "summary": {},
                "sessions": [object()],
                "trend": [],
                "servers": [],
                "logs": [],
                "poll_interval": 5,
            }

        admin_routes._dashboard_snapshot = invalid_snapshot
        try:
            response = self.client.get("/api/v1/dashboard")
        finally:
            admin_routes._dashboard_snapshot = original_snapshot

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json(),
            {
                "ok": False,
                "data": None,
                "error": "总览数据暂时不可用，请稍后重试",
            },
        )


if __name__ == "__main__":
    unittest.main()
