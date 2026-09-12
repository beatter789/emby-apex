import asyncio
import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch


_temp_dir = tempfile.TemporaryDirectory(prefix="emby-apex-portal-api-")
os.environ["APEX_DATA"] = str(Path(_temp_dir.name) / "data")
os.environ["APEX_IMAGE"] = str(Path(_temp_dir.name) / "image")

from fastapi.testclient import TestClient  # noqa: E402

from app import services  # noqa: E402
from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.models import ManagedUser, MediaRequest, Server, utcnow  # noqa: E402
from sqlalchemy import select  # noqa: E402
from app.portal_main import app  # noqa: E402
from app.security import hash_password  # noqa: E402


@asynccontextmanager
async def _test_lifespan(_app):
    yield


class PortalRequestsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())
        cls.original_lifespan = app.router.lifespan_context
        app.router.lifespan_context = _test_lifespan

    @classmethod
    def tearDownClass(cls):
        app.router.lifespan_context = cls.original_lifespan
        asyncio.run(engine.dispose())
        # app.db.engine is process-global and later unittest modules may have
        # imported it already.  Do not remove its database path here; doing so
        # leaves the shared engine pointing at a deleted directory and causes
        # unrelated tests to fail with "unable to open database file".

    def setUp(self):
        self.user_id, self.other_user_id = asyncio.run(self._reset_data())
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    async def _reset_data(self) -> tuple[int, int]:
        async with engine.begin() as conn:
            for table in reversed(list(Server.metadata.sorted_tables)):
                await conn.execute(table.delete())
        async with SessionLocal() as db:
            server = Server(
                name="api-server",
                base_url="http://api-server",
                api_key_encrypted="test",
                enabled=True,
            )
            other_server = Server(
                name="other-server",
                base_url="http://other-server",
                api_key_encrypted="test",
                enabled=True,
            )
            db.add_all([server, other_server])
            await db.flush()
            user = ManagedUser(
                server_id=server.id,
                emby_user_id="user-1",
                username="portal-user",
                portal_enabled=True,
                portal_password_hash=hash_password("password"),
                activated_at=utcnow(),
            )
            same_server_user = ManagedUser(
                server_id=server.id,
                emby_user_id="user-1b",
                username="same-server-user",
                portal_enabled=True,
                portal_password_hash=hash_password("password"),
                activated_at=utcnow(),
            )
            other_user = ManagedUser(
                server_id=other_server.id,
                emby_user_id="user-2",
                username="other-user",
                portal_enabled=True,
                portal_password_hash=hash_password("password"),
                activated_at=utcnow(),
            )
            db.add_all([user, same_server_user, other_user])
            await db.flush()
            db.add_all(
                [
                    MediaRequest(
                        server_id=server.id,
                        managed_user_id=user.id,
                        tmdb_id=101,
                        media_type="movie",
                        title="自己的待处理",
                        status="pending",
                    ),
                    MediaRequest(
                        server_id=server.id,
                        managed_user_id=user.id,
                        tmdb_id=102,
                        media_type="movie",
                        title="本服已入库",
                        status="in_library",
                        confirmed_at=utcnow(),
                    ),
                    MediaRequest(
                        server_id=server.id,
                        managed_user_id=same_server_user.id,
                        tmdb_id=103,
                        media_type="movie",
                        title="同服他人已入库",
                        status="in_library",
                        note="这是其他用户的私密备注",
                        created_at=utcnow(),
                        confirmed_at=utcnow(),
                    ),
                    MediaRequest(
                        server_id=other_server.id,
                        managed_user_id=other_user.id,
                        tmdb_id=201,
                        media_type="tv",
                        title="其他服务器记录",
                        status="pending",
                    ),
                ]
            )
            await db.commit()
            return user.id, other_user.id

    def _login_session(self, *, username: str = "portal-user") -> str:
        csrf_token = self.client.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
        login = self.client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": "password", "csrf_token": csrf_token},
        )
        self.assertEqual(login.status_code, 200)
        account = self.client.get("/api/v1/account")
        self.assertEqual(account.status_code, 200)
        return account.json()["data"]["csrf_token"]

    def test_unauthenticated_api_returns_json_401(self):
        response = self.client.get("/api/v1/requests", follow_redirects=False)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json(),
            {"ok": False, "data": None, "error": "未登录或登录已失效"},
        )
        self.assertNotIn("location", response.headers)

    def test_request_lists_are_scoped_to_current_user_and_server(self):
        self._login_session()
        response = self.client.get("/api/v1/requests")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["error"])
        self.assertEqual(
            [item["title"] for item in payload["data"]["pending"]],
            ["自己的待处理"],
        )
        self.assertEqual(
            {item["title"] for item in payload["data"]["in_library"]},
            {"同服他人已入库", "本服已入库"},
        )
        shared = next(
            item
            for item in payload["data"]["in_library"]
            if item["title"] == "同服他人已入库"
        )
        self.assertNotIn("note", shared)
        self.assertNotIn("created_at", shared)
        self.assertNotIn("rejection_reason", shared)
        self.assertNotIn("这是其他用户的私密备注", response.text)
        self.assertEqual(payload["data"]["rejected"], [])
        self.assertNotIn("其他服务器记录", response.text)

    def test_create_request_rejects_missing_or_wrong_csrf(self):
        self._login_session()
        for payload, headers in (
            ({"tmdb_id": 103, "media_type": "movie", "note": "test"}, {}),
            (
                {
                    "tmdb_id": 103,
                    "media_type": "movie",
                    "note": "test",
                    "csrf_token": "wrong",
                },
                {},
            ),
        ):
            response = self.client.post(
                "/api/v1/requests",
                json=payload,
                headers=headers,
            )
            self.assertEqual(response.status_code, 400)
            self.assertFalse(response.json()["ok"])
            self.assertIn("CSRF", response.json()["error"])

    def test_search_success_and_error_use_common_envelope(self):
        original_search = services.search_tmdb

        async def fake_search(mode: str, query: str, year: str = ""):
            if query == "error":
                raise services.RegistrationError("连接 TMDB 失败，请检查网络或代理设置")
            return [
                {
                    "tmdb_id": 616037,
                    "media_type": "movie",
                    "title": "雷神4",
                    "original_title": "Thor: Love and Thunder",
                    "year": 2022,
                    "overview": "简介",
                    "poster_url": "https://image.tmdb.org/t/p/w342/test.jpg",
                }
            ]

        services.search_tmdb = fake_search
        try:
            self._login_session()
            response = self.client.get(
                "/api/v1/requests/search",
                params={"mode": "movie", "query": "雷神", "year": "2022"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["ok"])
            self.assertEqual(response.json()["data"]["results"][0]["tmdb_id"], 616037)
            self.assertIsNone(response.json()["error"])

            response = self.client.get(
                "/api/v1/requests/search",
                params={"mode": "movie", "query": "error"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(
                response.json(),
                {
                    "ok": False,
                    "data": None,
                    "error": "连接 TMDB 失败，请检查网络或代理设置",
                },
            )
        finally:
            services.search_tmdb = original_search

    def test_requests_route_is_static_vue_shell(self):
        self._login_session()
        html_response = self.client.get("/requests")
        self.assertEqual(html_response.status_code, 200)
        self.assertIn("text/html", html_response.headers["content-type"])
        self.assertIn("/portal.js", html_response.text)
        json_response = self.client.get("/requests?tab=pending", headers={"accept": "application/json"})
        self.assertEqual(json_response.status_code, 200)
        self.assertIn("/portal.js", json_response.text)

    def test_portal_tmdb_details_api_returns_full_payload_and_requires_login(self):
        response = self.client.get("/api/v1/requests/tmdb/movie/7")
        self.assertEqual(response.status_code, 401)
        self._login_session()
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
            "genres": ["剧情"],
            "rating": 8.0,
            "runtime_minutes": 100,
            "directors": [],
            "cast": [],
            "seasons": None,
            "episodes": None,
        }
        with patch("app.api.portal_requests.services.tmdb_details", new=AsyncMock(return_value=detail)):
            response = self.client.get("/api/v1/requests/tmdb/movie/7")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "data": detail, "error": None})

    def test_create_request_persists_detail_snapshot_from_search_result(self):
        self._login_session()
        detail = {
            "tmdb_id": 616037,
            "media_source": "themoviedb",
            "media_id": "616037",
            "media_type": "movie",
            "title": "雷神4",
            "original_title": "Thor: Love and Thunder",
            "year": 2022,
            "overview": "快照简介",
            "poster_url": "https://image.tmdb.org/t/p/w342/poster.jpg",
            "backdrop_url": "https://image.tmdb.org/t/p/w1280/backdrop.jpg",
            "rating": 7.1,
            "genres": ["动作"],
            "directors": [{"name": "导演"}],
            "cast": [{"name": "演员", "character": "角色"}],
        }
        with patch(
            "app.api.portal_requests.services.tmdb_details",
            new=AsyncMock(return_value={"tmdb_id": 616037, "media_type": "movie", "title": "616037"}),
        ):
            response = self.client.post(
                "/api/v1/requests",
                json={
                    "tmdb_id": 616037,
                    "media_id": "616037",
                    "media_source": "themoviedb",
                    "media_type": "movie",
                    "detail": detail,
                    "csrf_token": self._login_session(),
                },
            )
        self.assertEqual(response.status_code, 201, response.text)
        created_id = response.json()["data"]["id"]
        self.assertEqual(response.json()["data"]["title"], "雷神4")
        async def read_row():
            async with SessionLocal() as db:
                return await db.get(MediaRequest, created_id)
        row = asyncio.run(read_row())
        self.assertIsNotNone(row)
        snapshot = json.loads(row.detail_snapshot)
        self.assertEqual(snapshot["title"], "雷神4")
        self.assertEqual(snapshot["poster_url"], detail["poster_url"])

    def test_snapshot_reads_do_not_call_moviepilot(self):
        self._login_session()
        async def set_snapshot():
            async with SessionLocal() as db:
                row = await db.scalar(
                    select(MediaRequest).where(MediaRequest.title == "自己的待处理")
                )
                row.detail_snapshot = json.dumps({
                    "tmdb_id": 101,
                    "media_type": "movie",
                    "title": "数据库电影",
                    "poster_url": "https://example.test/poster.jpg",
                    "overview": "本地快照",
                }, ensure_ascii=False)
                await db.commit()
        asyncio.run(set_snapshot())
        with patch("app.api.portal_requests.services.moviepilot_details", new=AsyncMock(side_effect=AssertionError("unexpected upstream call"))) as upstream:
            lists = self.client.get("/api/v1/requests")
            self.assertEqual(lists.status_code, 200)
            self.assertEqual(lists.json()["data"]["pending"][0]["title"], "数据库电影")
            request_id = lists.json()["data"]["pending"][0]["id"]
            detail = self.client.get(f"/api/v1/requests/{request_id}")
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["data"]["poster_url"], "https://example.test/poster.jpg")
            upstream.assert_not_awaited()

    def test_legacy_detail_is_hydrated_once_then_read_from_database(self):
        self._login_session()
        async def request_id():
            async with SessionLocal() as db:
                row = await db.scalar(
                    select(MediaRequest).where(MediaRequest.title == "自己的待处理")
                )
                return row.id
        row_id = asyncio.run(request_id())
        detail = {
            "tmdb_id": 101,
            "media_type": "movie",
            "title": "旧记录电影",
            "poster_url": "https://example.test/legacy.jpg",
            "overview": "回填简介",
        }
        with patch("app.api.portal_requests.services.moviepilot_enabled", return_value=True), patch(
            "app.api.portal_requests.services.moviepilot_details", new=AsyncMock(return_value=detail)
        ) as upstream:
            first = self.client.get(f"/api/v1/requests/{row_id}")
            second = self.client.get(f"/api/v1/requests/{row_id}")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["data"]["title"], "旧记录电影")
        self.assertEqual(second.json()["data"]["title"], "旧记录电影")
        self.assertEqual(upstream.await_count, 1)


if __name__ == "__main__":
    unittest.main()
