import asyncio
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch


_temp_dir = tempfile.TemporaryDirectory(prefix="emby-apex-round2-api-")
os.environ["APEX_DATA"] = str(Path(_temp_dir.name) / "data")
os.environ["APEX_IMAGE"] = str(Path(_temp_dir.name) / "image")

from fastapi.testclient import TestClient  # noqa: E402

from app import services, settings_store  # noqa: E402
from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.main import app as admin_app  # noqa: E402
from app.models import ActionLog, AppSetting, Base, PlaybackRecord, RedeemCode, Server, ManagedUser, utcnow  # noqa: E402
from app.portal_main import app as portal_app  # noqa: E402
from app.security import hash_password  # noqa: E402


@asynccontextmanager
async def _test_lifespan(_app):
    yield


class Round2ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())
        cls.admin_lifespan = admin_app.router.lifespan_context
        cls.portal_lifespan = portal_app.router.lifespan_context
        admin_app.router.lifespan_context = _test_lifespan
        portal_app.router.lifespan_context = _test_lifespan

    @classmethod
    def tearDownClass(cls):
        admin_app.router.lifespan_context = cls.admin_lifespan
        portal_app.router.lifespan_context = cls.portal_lifespan

    def setUp(self):
        asyncio.run(self._reset_data())
        self.admin = TestClient(admin_app)
        self.portal = TestClient(portal_app)
        self.admin.__enter__()
        self.portal.__enter__()

    def tearDown(self):
        self.admin.__exit__(None, None, None)
        self.portal.__exit__(None, None, None)

    async def _reset_data(self):
        async with engine.begin() as conn:
            for table in reversed(list(Base.metadata.sorted_tables)):
                await conn.execute(table.delete())

    def _admin_login(self):
        token = self.admin.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
        response = self.admin.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "admin", "csrf_token": token},
        )
        self.assertEqual(response.status_code, 200)
        return self.admin.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]

    async def _seed_portal_user(self):
        async with SessionLocal() as db:
            server = Server(name="round2", base_url="http://round2", api_key_encrypted="x")
            db.add(server)
            await db.flush()
            user = ManagedUser(
                server_id=server.id,
                emby_user_id="portal-1",
                username="round2-user",
                portal_enabled=True,
                portal_password_hash=hash_password("password"),
                activated_at=utcnow(),
            )
            db.add(user)
            await db.flush()
            db.add_all(
                [
                    RedeemCode(
                        code="USED-ONE",
                        duration_seconds=86400,
                        amount=1,
                        unit="day",
                        used_by_user_id=user.id,
                        used_by_username=user.username,
                        used_at=utcnow(),
                    ),
                    RedeemCode(
                        code="OTHER-USER",
                        duration_seconds=3600,
                        amount=1,
                        unit="hour",
                        used_by_user_id=None,
                        used_by_username="other",
                        used_at=utcnow(),
                    ),
                ]
            )
            await db.commit()

    def _portal_login(self):
        token = self.portal.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
        response = self.portal.post(
            "/api/v1/auth/login",
            json={"username": "round2-user", "password": "password", "csrf_token": token},
        )
        self.assertEqual(response.status_code, 200)

    def test_history_empty_date_filter_and_safe_exception_envelope(self):
        self._admin_login()
        response = self.admin.get("/api/v1/history?days=7")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "data": {"days": 7, "records": []}, "error": None})

        async def seed():
            async with SessionLocal() as db:
                server = Server(name="history-server", base_url="http://history", api_key_encrypted="x")
                db.add(server)
                await db.flush()
                db.add(
                    PlaybackRecord(
                        server_id=server.id,
                        session_key="history-1",
                        emby_user_id="u1",
                        username="u1",
                        item_name="recent",
                        client="web",
                        started_at=datetime.now(timezone.utc) - timedelta(days=1),
                    )
                )
                await db.commit()

        asyncio.run(seed())
        response = self.admin.get("/api/v1/history?days=2")
        self.assertEqual(response.status_code, 200)
        record = response.json()["data"]["records"][0]
        self.assertEqual(record["item_name"], "recent")
        self.assertNotRegex(record["started_at"], r"\.\d+")

        with patch("app.api.admin.stats.recent_records", side_effect=RuntimeError("raw secret")):
            response = self.admin.get("/api/v1/history?days=bad")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(set(response.json()), {"ok", "data", "error"})
        self.assertFalse(response.json()["ok"])
        self.assertIsNone(response.json()["data"])
        self.assertNotIn("raw secret", response.text)

    def test_settings_connection_tests_require_csrf_and_return_safe_envelopes(self):
        self._admin_login()
        for path in (
            "/api/v1/settings/tmdb/test",
            "/api/v1/settings/wecom/test",
            "/api/v1/settings/notifications/test",
        ):
            response = self.admin.post(path, json={})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(set(response.json()), {"ok", "data", "error"})

        class FakeTmdb:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def test_connection(self):
                return None

        token = self.admin.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
        with patch("app.api.admin.TmdbClient", return_value=FakeTmdb()):
            response = self.admin.post(
                "/api/v1/settings/tmdb/test",
                json={"csrf_token": token, "tmdb_api_key": "raw-key", "tmdb_proxy_url": "http://user:secret@proxy"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["message"], "TMDB 连接成功")
        self.assertNotIn("raw-key", response.text)
        self.assertNotIn("secret", response.text)

        with patch("app.api.admin.notify.test_webhook", new=AsyncMock(side_effect=RuntimeError("raw-token"))):
            response = self.admin.post(
                "/api/v1/settings/notifications/test",
                json={"csrf_token": token, "channel": "webhook", "notify_webhook_url": "https://example.test/raw-secret"},
            )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "通知连接测试失败，请稍后重试")
        self.assertNotIn("raw-token", response.text)

    def test_portal_redeem_codes_are_scoped_and_use_second_precision(self):
        asyncio.run(self._seed_portal_user())
        self._portal_login()
        response = self.portal.get("/api/v1/account/redeem-codes")
        self.assertEqual(response.status_code, 200)
        codes = response.json()["data"]["codes"]
        self.assertEqual(len(codes), 1)
        self.assertEqual(codes[0]["code"], "USED-ONE")
        self.assertEqual(codes[0]["duration"], "1 天")
        self.assertNotRegex(codes[0]["used_at"], r"\.\d+")
        self.assertNotIn("OTHER-USER", response.text)

    def test_user_timestamps_use_iso_seconds(self):
        async def seed():
            async with SessionLocal() as db:
                server = Server(name="users-time", base_url="http://users-time", api_key_encrypted="x")
                db.add(server)
                await db.flush()
                stamp = datetime.now(timezone.utc).replace(microsecond=654321)
                db.add(
                    ManagedUser(
                        server_id=server.id,
                        emby_user_id="time-user",
                        username="time-user",
                        portal_enabled=True,
                        activated_at=stamp,
                        registered_at=stamp,
                        synced_at=stamp,
                        expires_at=stamp,
                    )
                )
                await db.commit()

        asyncio.run(seed())
        self._admin_login()
        response = self.admin.get("/api/v1/users")
        self.assertEqual(response.status_code, 200)
        row = response.json()["data"]["users"][0]
        for field in ("expires_at", "registered_at", "synced_at"):
            self.assertRegex(row[field], r"T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?$")
            self.assertNotRegex(row[field], r"\.\d+")

    def test_policy_log_details_are_compact_translated_and_redacted(self):
        async def seed():
            async with SessionLocal() as db:
                db.add(
                    ActionLog(
                        action="user_policy_updated",
                        detail='{"EnableMediaPlayback":true,"EnableVideoPlaybackTranscoding":false,"api_key":"raw-key","EnabledFolders":["movies","tv"]}',
                        created_at=utcnow(),
                    )
                )
                await db.commit()

        asyncio.run(seed())
        self._admin_login()
        response = self.admin.get("/api/v1/logs")
        self.assertEqual(response.status_code, 200)
        detail = response.json()["data"]["logs"][0]["detail"]
        self.assertIn("允许媒体播放=开启", detail)
        self.assertIn("视频转码=关闭", detail)
        self.assertIn("指定媒体库=2 项", detail)
        self.assertNotIn("raw-key", response.text)
        self.assertNotIn('"EnableMediaPlayback"', detail)
        self.assertNotRegex(response.json()["data"]["logs"][0]["created_at"], r"\.\d+")

    def test_portal_does_not_expose_admin_shell_routes(self):
        response = self.portal.get("/settings")
        self.assertEqual(response.status_code, 404)

    def test_portal_registration_reads_latest_database_setting(self):
        async def disable_registration_in_db():
            async with SessionLocal() as db:
                db.add(AppSetting(key="registration_enabled", value="false"))
                await db.commit()

        asyncio.run(disable_registration_in_db())
        original = settings_store._cache
        settings_store._cache = original.__class__(
            **{**original.__dict__, "registration_enabled": True}
        )
        try:
            csrf = self.portal.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
            response = self.portal.post(
                "/api/v1/auth/register",
                json={
                    "username": "new-user",
                    "password": "password",
                    "password2": "password",
                    "csrf_token": csrf,
                },
            )
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"], "当前未开放注册")
        finally:
            settings_store._cache = original

    def test_patch_user_fields_persist_with_second_precision_and_null_expiry(self):
        async def seed():
            async with SessionLocal() as db:
                server = Server(name="patch-server", base_url="http://patch", api_key_encrypted="x")
                db.add(server)
                await db.flush()
                user = ManagedUser(
                    server_id=server.id,
                    emby_user_id="patch-user",
                    username="patch-user",
                    portal_enabled=True,
                    activated_at=utcnow(),
                )
                db.add(user)
                await db.commit()
                return user.id

        user_id = asyncio.run(seed())
        token = self._admin_login()
        missing_csrf = self.admin.patch(
            f"/api/v1/users/{user_id}",
            json={"note": "must-not-save"},
        )
        self.assertEqual(missing_csrf.status_code, 400)
        self.assertIn("CSRF", missing_csrf.json()["error"])
        response = self.admin.patch(
            f"/api/v1/users/{user_id}",
            json={
                "csrf_token": token,
                "expires_at": "2035-01-02T03:04:05",
                "note": "持久化备注",
                "client_policy_mode": "allow",
                "client_patterns": ["iOS", "web"],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

        async def read_user():
            async with SessionLocal() as db:
                return await db.get(ManagedUser, user_id)

        stored = asyncio.run(read_user())
        self.assertIsNotNone(stored)
        self.assertEqual(stored.expires_at.replace(tzinfo=timezone.utc).microsecond, 0)
        self.assertEqual(stored.note, "持久化备注")
        self.assertEqual(stored.client_policy_mode, "allow")
        self.assertEqual(stored.client_patterns, ["iOS", "web"])

        response = self.admin.patch(
            f"/api/v1/users/{user_id}",
            json={"csrf_token": token, "expires_at": None},
        )
        self.assertEqual(response.status_code, 200)
        stored = asyncio.run(read_user())
        self.assertIsNone(stored.expires_at)

        response = self.admin.patch(
            f"/api/v1/users/{user_id}",
            json={"csrf_token": token, "expires_at": "not-a-time"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()), {"ok", "data", "error"})
        self.assertFalse(response.json()["ok"])
        self.assertEqual(response.json()["error"], "到期时间格式不正确")
        self.assertIsNone(asyncio.run(read_user()).expires_at)

    def test_admin_tmdb_details_api_uses_full_detail_serializer(self):
        self._admin_login()
        detail = {
            "tmdb_id": 7,
            "media_type": "movie",
            "title": "详情",
            "original_title": "Detail",
            "year": 2025,
            "release_date": "2025-01-02",
            "tagline": "宣传语",
            "status": "Released",
            "overview": "简介",
            "poster_url": "https://image.tmdb.org/t/p/w342/poster.jpg",
            "backdrop_url": "https://image.tmdb.org/t/p/w1280/backdrop.jpg",
            "genres": ["动作"],
            "rating": 8.1,
            "runtime_minutes": 120,
            "directors": [{"id": 1, "name": "导演"}],
            "cast": [{"id": 2, "name": "演员", "character": "角色", "profile_url": None}],
            "seasons": None,
            "episodes": None,
        }
        with patch("app.api.admin.services.tmdb_details", new=AsyncMock(return_value=detail)):
            response = self.admin.get("/api/v1/requests/tmdb/movie/7")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], detail)

    def test_admin_tmdb_details_requires_admin_session(self):
        response = self.admin.get("/api/v1/requests/tmdb/movie/7")
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
