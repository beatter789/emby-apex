import asyncio
import logging
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


_temp_dir = tempfile.TemporaryDirectory(prefix="emby-apex-settings-api-")
os.environ["APEX_DATA"] = str(Path(_temp_dir.name) / "data")
os.environ["APEX_IMAGE"] = str(Path(_temp_dir.name) / "image")

from fastapi.testclient import TestClient  # noqa: E402

from app import scheduler, settings_store  # noqa: E402
from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, Server  # noqa: E402
from app.wecom import WeComError  # noqa: E402


@asynccontextmanager
async def _test_lifespan(_app):
    yield


class SettingsApiTests(unittest.TestCase):
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
        # Keep the process-wide runtime cache isolated from later test modules.
        settings_store._cache = settings_store._from_env()
        from app.logging_config import set_log_level

        set_log_level(settings_store.current().log_level)

    async def _reset_data(self):
        async with engine.begin() as conn:
            for table in reversed(list(Base.metadata.sorted_tables)):
                await conn.execute(table.delete())

    def _login(self):
        token = self.client.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
        response = self.client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "admin", "csrf_token": token},
        )
        self.assertEqual(response.status_code, 200)
        return self.client.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]

    def test_requires_admin_session(self):
        response = self.client.get("/api/v1/settings")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(set(response.json()), {"ok", "data", "error"})
        self.assertFalse(response.json()["ok"])
        self.assertIsNone(response.json()["data"])

    def test_encoding_aes_key_variants_are_never_serialized(self):
        runtime = SimpleNamespace(
            allow_emby_self_password=False,
            portal_password_min_length=8,
        )
        raw_values = {
            "encoding_aes_key": "RAW-AES-SECRET",
            "EncodingAESKey": "RAW-AES-CAMEL",
            "encoding-aes-key": "RAW-AES-HYPHEN",
            "wecom_encoding_aes_key": "RAW-WECOM-AES",
            "WeCom-Encoding-AES-Key": "RAW-WECOM-HYPHEN",
        }
        for name, value in raw_values.items():
            setattr(runtime, name, value)
        rows = [
            {
                "name": name,
                "kind": "str",
                "min": None,
                "max": None,
                "label": name,
                "hint": "",
                "value": value,
                "configured": True,
            }
            for name, value in raw_values.items()
        ]

        async def fake_load(_db):
            return runtime

        self._login()
        with patch.object(settings_store, "load", new=fake_load), patch.object(
            settings_store, "current", return_value=runtime
        ), patch.object(settings_store, "form_rows", return_value=rows):
            response = self.client.get("/api/v1/settings")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        response_text = response.text
        for raw in raw_values.values():
            self.assertNotIn(raw, response_text)
        self.assertFalse(any(name in body["data"]["settings"] for name in raw_values))
        for row in body["data"]["fields"]:
            self.assertEqual(row["value"], "")
            self.assertTrue(row["configured"])
            self.assertTrue(row["sensitive"])

    def test_password_named_options_are_readable_and_writable(self):
        token = self._login()
        response = self.client.patch(
            "/api/v1/settings",
            json={
                "allow_emby_self_password": True,
                "portal_password_min_length": 12,
                "csrf_token": token,
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["settings"]["allow_emby_self_password"])
        self.assertEqual(data["settings"]["portal_password_min_length"], 12)

        fields = {row["name"]: row for row in data["fields"]}
        self.assertTrue(fields["allow_emby_self_password"]["value"])
        self.assertEqual(fields["portal_password_min_length"]["value"], 12)
        self.assertFalse(fields["allow_emby_self_password"].get("sensitive", False))
        self.assertFalse(fields["portal_password_min_length"].get("sensitive", False))

        response = self.client.get("/api/v1/settings")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["settings"]["allow_emby_self_password"])
        self.assertEqual(data["settings"]["portal_password_min_length"], 12)

    def test_log_level_is_runtime_setting_and_defaults_to_warning(self):
        token = self._login()
        response = self.client.get("/api/v1/settings")
        self.assertEqual(response.status_code, 200)
        fields = {row["name"]: row for row in response.json()["data"]["fields"]}
        self.assertEqual(fields["log_level"]["value"], "WARNING")
        response = self.client.patch(
            "/api/v1/settings",
            json={"log_level": "INFO", "csrf_token": token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["settings"]["log_level"], "INFO")
        self.assertEqual(logging.getLogger().level, logging.INFO)
        response = self.client.patch(
            "/api/v1/settings",
            json={"log_level": "invalid", "csrf_token": token},
        )
        self.assertEqual(response.status_code, 400)

    def test_versioned_wecom_menu_api_is_mounted_and_secure(self):
        # The former HTML endpoints are intentionally not registered.
        self.assertEqual(self.client.get("/settings/wecom/menu").status_code, 404)
        self.assertEqual(self.client.post("/settings/wecom/menu/sync").status_code, 404)

        response = self.client.get("/api/v1/settings/wecom/menu")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(set(response.json()), {"ok", "data", "error"})

        token = self._login()
        for path in (
            "/api/v1/settings/wecom/menu/sync",
            "/api/v1/settings/wecom/menu/delete",
        ):
            response = self.client.post(path, json={})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(set(response.json()), {"ok", "data", "error"})

        with patch("app.api.admin.notify.sync_wecom_menu", new=AsyncMock()) as sync:
            response = self.client.post(
                "/api/v1/settings/wecom/menu/sync", json={"csrf_token": token}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "data": {"message": "企业微信应用菜单已同步"}, "error": None})
        sync.assert_awaited_once()

        menu = {"button": [{"name": "用户管理", "sub_button": []}]}
        with patch("app.api.admin.notify.get_wecom_menu", new=AsyncMock(return_value=menu)) as get_menu:
            response = self.client.get("/api/v1/settings/wecom/menu")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "data": {"menu": menu}, "error": None})
        get_menu.assert_awaited_once()

        with patch("app.api.admin.notify.delete_wecom_menu", new=AsyncMock()) as delete:
            response = self.client.post(
                "/api/v1/settings/wecom/menu/delete", json={"csrf_token": token}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "data": {"message": "企业微信应用菜单已删除"}, "error": None})
        delete.assert_awaited_once()

        with patch("app.api.admin.notify.delete_wecom_menu", new=AsyncMock()) as delete:
            response = self.client.request(
                "DELETE", "/api/v1/settings/wecom/menu/delete", json={"csrf_token": token}
            )
        self.assertEqual(response.status_code, 200)
        delete.assert_awaited_once()

        with patch(
            "app.api.admin.notify.sync_wecom_menu",
            new=AsyncMock(side_effect=WeComError("access_token=raw-secret")),
        ):
            response = self.client.post(
                "/api/v1/settings/wecom/menu/sync", json={"csrf_token": token}
            )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("raw-secret", response.text)
        self.assertEqual(set(response.json()), {"ok", "data", "error"})

    def test_tmdb_api_key_is_visible_only_in_admin_settings(self):
        runtime = SimpleNamespace(
            tmdb_api_key="plain-tmdb-key",
            allow_emby_self_password=False,
            portal_password_min_length=8,
        )
        rows = [
            {
                "name": "tmdb_api_key",
                "kind": "str",
                "min": None,
                "max": None,
                "label": "TMDB API Key",
                "hint": "",
                "value": "plain-tmdb-key",
                "configured": True,
            }
        ]

        async def fake_load(_db):
            return runtime

        self._login()
        with patch.object(settings_store, "load", new=fake_load), patch.object(
            settings_store, "current", return_value=runtime
        ), patch.object(settings_store, "form_rows", return_value=rows):
            response = self.client.get("/api/v1/settings")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["data"]["settings"]["tmdb_api_key"], "plain-tmdb-key")
        field = next(row for row in body["data"]["fields"] if row["name"] == "tmdb_api_key")
        self.assertEqual(field["value"], "plain-tmdb-key")
        self.assertFalse(field.get("sensitive", False))

    def test_registration_target_is_returned_and_saved_with_registration_toggle(self):
        async def seed_server():
            async with SessionLocal() as db:
                server = Server(
                    name="register-target",
                    base_url="http://register-target",
                    api_key_encrypted="x",
                    enabled=True,
                )
                db.add(server)
                await db.commit()
                await db.refresh(server)
                return server.id

        server_id = asyncio.run(seed_server())
        token = self._login()
        response = self.client.patch(
            "/api/v1/settings",
            json={
                "registration_enabled": True,
                "registration_server_id": server_id,
                "csrf_token": token,
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["settings"]["registration_enabled"])
        self.assertEqual(data["registration_server_id"], server_id)
        self.assertEqual(data["registration_servers"][0]["id"], server_id)

        response = self.client.get("/api/v1/settings")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["registration_server_id"], server_id)

    def test_activation_notification_switch_uses_nested_settings_contract(self):
        token = self._login()
        response = self.client.patch(
            "/api/v1/settings",
            json={"settings": {"activation": {"webhook": False, "telegram": False, "wecom": False}}, "csrf_token": token},
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["settings"]["activation"], {"webhook": False, "telegram": False, "wecom": False})
        fields = {row["name"]: row for row in data["fields"]}
        self.assertFalse(fields["activation.wecom"]["value"])


if __name__ == "__main__":
    unittest.main()
