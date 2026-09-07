import asyncio
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4


_temp_dir = tempfile.TemporaryDirectory(prefix="emby-controller-tests-")
os.environ["APEX_DATA"] = str(Path(_temp_dir.name) / "data")
os.environ["APEX_IMAGE"] = str(Path(_temp_dir.name) / "image")

from app import services  # noqa: E402
from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.emby import PLAYBACK_POLICY_FIELDS  # noqa: E402
from app.models import ManagedUser, MediaRequest, RedeemCode, Server, utcnow  # noqa: E402
from app.portal_routes import _describe_status  # noqa: E402


class FakeEmbyClient:
    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.policies: dict[str, dict] = {}
        self.next_id = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def list_users(self):
        return [
            {"Id": user_id, "Name": self.names[user_id], "Policy": dict(policy)}
            for user_id, policy in self.policies.items()
        ]

    async def create_user(self, name: str):
        user_id = f"u{self.next_id}"
        self.next_id += 1
        self.names[user_id] = name
        self.policies[user_id] = {
            "IsDisabled": False,
            "IsAdministrator": False,
            "EnableMediaPlayback": False,
            "UnknownField": "keep",
        }
        return {"Id": user_id}

    async def set_password(self, _user_id: str, _password: str):
        return None

    async def verify_credentials(self, _username: str, _password: str):
        return True

    async def apply_restricted_policy(
        self, user_id: str, *, allow_playback: bool, allow_self_password: bool = False
    ):
        self.policies[user_id]["EnableMediaPlayback"] = allow_playback
        self.policies[user_id]["EnableUserPreferenceAccess"] = allow_self_password
        for field in PLAYBACK_POLICY_FIELDS:
            self.policies[user_id][field] = False

    async def set_playback_allowed(self, user_id: str, allowed: bool):
        for field in PLAYBACK_POLICY_FIELDS:
            self.policies[user_id][field] = False
        self.policies[user_id]["EnableMediaPlayback"] = allowed

    async def set_user_disabled(self, user_id: str, disabled: bool):
        self.policies[user_id]["IsDisabled"] = disabled

    async def list_sessions(self):
        return []

    async def set_policy_fields(self, user_id: str, changes: dict):
        self.policies[user_id].update(changes)

    async def get_user(self, user_id: str):
        return {
            "Id": user_id,
            "Name": self.names[user_id],
            "Policy": dict(self.policies[user_id]),
        }

    async def delete_user(self, user_id: str):
        self.names.pop(user_id, None)
        self.policies.pop(user_id, None)


class UserPermissionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def tearDownClass(cls):
        asyncio.run(engine.dispose())
        _temp_dir.cleanup()

    async def asyncSetUp(self):
        await init_db()
        self.fake = FakeEmbyClient()
        self.original_client_for = services.client_for
        services.client_for = lambda _server: self.fake

    async def asyncTearDown(self):
        services.client_for = self.original_client_for

    async def test_create_modes_and_playback_sources(self):
        async with SessionLocal() as db:
            server = Server(
                name="mock",
                base_url="http://mock",
                api_key_encrypted="test",
                enabled=True,
            )
            db.add(server)
            await db.commit()
            await db.refresh(server)

            ordinary = await services.create_managed_user(
                db,
                server=server,
                username="ordinary",
                password="12345678",
                ordinary_registration=True,
                allow_playback=True,
                expires_at=utcnow() + timedelta(days=1),
            )
            self.assertTrue(ordinary.self_registered)
            self.assertTrue(ordinary.portal_enabled)
            self.assertIsNone(ordinary.activated_at)
            self.assertIsNone(ordinary.expires_at)
            self.assertEqual(ordinary.playback_source, "controller")
            self.assertFalse(ordinary.playback_enabled)

            direct = await services.create_managed_user(
                db,
                server=server,
                username="directuser",
                password="12345678",
                ordinary_registration=False,
                allow_playback=True,
                expires_at=utcnow() + timedelta(days=30),
            )
            self.assertFalse(direct.self_registered)
            self.assertTrue(direct.portal_enabled)
            self.assertIsNotNone(direct.activated_at)
            self.assertIsNotNone(direct.expires_at)
            self.assertEqual(direct.playback_source, "controller")
            self.assertTrue(direct.playback_enabled)
            self.assertFalse(
                self.fake.policies[direct.emby_user_id][
                    "EnableVideoPlaybackTranscoding"
                ]
            )
            self.assertFalse(
                self.fake.policies[direct.emby_user_id]["EnablePlaybackRemuxing"]
            )

            old = ManagedUser(
                server_id=server.id,
                emby_user_id="old1",
                username="olduser",
                portal_enabled=True,
                portal_password_hash="test",
                playback_source="emby",
                playback_enabled=False,
            )
            self.fake.names["old1"] = "olduser"
            self.fake.policies["old1"] = {
                "IsDisabled": True,
                "IsAdministrator": False,
                "EnableMediaPlayback": True,
                "UnknownField": "keep",
            }
            db.add(old)
            await db.commit()
            await services.refresh_user_from_emby(db, old)
            self.assertTrue(old.is_disabled)
            self.assertTrue(old.playback_enabled)

            self.fake.policies[direct.emby_user_id]["EnableMediaPlayback"] = True
            self.fake.policies[direct.emby_user_id][
                "EnableVideoPlaybackTranscoding"
            ] = True
            direct.playback_enabled = False
            await db.commit()
            await services.refresh_user_from_emby(db, direct)
            self.assertFalse(direct.playback_enabled)
            self.assertFalse(
                self.fake.policies[direct.emby_user_id][
                    "EnableVideoPlaybackTranscoding"
                ]
            )

            await services.update_user_policy(
                db, direct, {"EnableRemoteAccess": True}
            )
            self.assertEqual(
                services.policy_for_user(direct)["UnknownField"], "keep"
            )

            # The page is now the static Vue shell; API contracts are tested
            # separately and no test should render removed Jinja templates.
            root = Path(__file__).resolve().parents[1]
            admin_shell = (root / "app/static/frontend/admin.html").read_text(encoding="utf-8")
            portal_shell = (root / "app/static/frontend/portal.html").read_text(encoding="utf-8")
            self.assertIn("/static/frontend/admin.js", admin_shell)
            self.assertIn("/static/frontend/portal.js", portal_shell)
            self.assertIn("getApi", (root / "frontend/src/admin/AdminApp.vue").read_text(encoding="utf-8"))
            self.assertIn("/account/status", (root / "frontend/src/portal/AccountApp.vue").read_text(encoding="utf-8"))

    async def test_registration_and_claim_survive_notification_failure(self):
        notifications = 0

        async def failed_notification(title, message, **_kwargs):
            nonlocal notifications
            notifications += 1
            content = f"{title}\n{message}"
            self.assertNotIn("12345678", content)
            self.assertNotIn("secret-token", content)
            raise RuntimeError("secret-token")

        original_push = services.notify.push
        services.notify.push = failed_notification
        original_runtime = services.settings_store.current()
        services.settings_store._cache = original_runtime.__class__(
            **{**original_runtime.__dict__, "claim_enabled": True}
        )
        try:
            async with SessionLocal() as db:
                server = Server(
                    name=f"notification-registration-{uuid4().hex}",
                    base_url="http://mock",
                    api_key_encrypted="test",
                    enabled=True,
                    is_register_target=True,
                )
                db.add(server)
                await db.flush()

                registered = await services.register_user(
                    db, username=f"self{uuid4().hex[:8]}", password="12345678"
                )
                self.assertTrue(registered.self_registered)
                self.assertIsNotNone(await db.get(ManagedUser, registered.id))

                direct = await services.create_managed_user(
                    db,
                    server=server,
                    username=f"direct{uuid4().hex[:8]}",
                    password="12345678",
                    ordinary_registration=False,
                    allow_playback=True,
                    expires_at=None,
                )
                self.assertTrue(direct.playback_enabled)
                self.assertIsNotNone(await db.get(ManagedUser, direct.id))

                old = ManagedUser(
                    server_id=server.id,
                    emby_user_id="claimed-notification",
                    username="claimed-notification",
                    portal_enabled=False,
                    playback_source="emby",
                )
                db.add(old)
                self.fake.names[old.emby_user_id] = old.username
                self.fake.policies[old.emby_user_id] = {
                    "IsDisabled": False,
                    "IsAdministrator": False,
                    "EnableMediaPlayback": True,
                }
                await db.commit()
                claimed = await services._claim_existing_user(
                    db,
                    server=server,
                    existing=old,
                    username=old.username,
                    password="12345678",
                )
                self.assertTrue(claimed.portal_enabled)
                self.assertIsNotNone(await db.get(ManagedUser, old.id))

                approved = ManagedUser(
                    server_id=server.id,
                    emby_user_id="approved-notification",
                    username="approved-notification",
                    portal_enabled=False,
                    playback_source="emby",
                )
                db.add(approved)
                self.fake.names[approved.emby_user_id] = approved.username
                self.fake.policies[approved.emby_user_id] = {
                    "IsDisabled": False,
                    "IsAdministrator": False,
                    "EnableMediaPlayback": True,
                }
                await db.commit()
                await services.enable_portal_login(db, approved, "12345678")
                self.assertTrue(approved.portal_enabled)

                code = RedeemCode(
                    code=f"TEST-{uuid4().hex[:12].upper()}",
                    duration_seconds=86400,
                    amount=1,
                    unit="day",
                )
                db.add(code)
                await db.commit()
                redeemed = await services.redeem_code(db, registered, code.code)
                self.assertIsNotNone(redeemed.used_at)
                self.assertTrue(registered.playback_enabled)

                direct_id = direct.id
                await services.admin_delete_user(db, direct)
                self.assertIsNone(await db.get(ManagedUser, direct_id))

                with self.assertRaises(services.RegistrationError):
                    await services.register_user(db, username="x", password="12345678")
                self.assertEqual(notifications, 6)
        finally:
            services.notify.push = original_push
            services.settings_store._cache = original_runtime

    async def test_expiry_and_recycle_notifications_are_idempotent(self):
        calls: list[tuple[str, str]] = []

        async def capture_notification(title, message, *, category="general"):
            calls.append((title, category))
            raise RuntimeError("notification-failed")

        original_push = services.notify.push
        services.notify.push = capture_notification
        original_runtime = services.settings_store.current()
        services.settings_store._cache = original_runtime.__class__(
            **{**original_runtime.__dict__, "expiry_remind_days": 3, "purge_after_expiry_days": 7}
        )
        try:
            async with SessionLocal() as db:
                server = Server(
                    name=f"notification-expiry-{uuid4().hex}",
                    base_url="http://mock",
                    api_key_encrypted="test",
                    enabled=True,
                )
                db.add(server)
                await db.flush()
                expired = ManagedUser(
                    server_id=server.id,
                    emby_user_id="expired-notification",
                    username="expired-notification",
                    portal_enabled=True,
                    portal_password_hash="hash",
                    playback_enabled=True,
                    playback_source="controller",
                    expires_at=utcnow() - timedelta(minutes=1),
                )
                reminder = ManagedUser(
                    server_id=server.id,
                    emby_user_id="reminder-notification",
                    username="reminder-notification",
                    portal_enabled=True,
                    playback_enabled=True,
                    playback_source="controller",
                    expires_at=utcnow() + timedelta(days=1),
                )
                disabled = ManagedUser(
                    server_id=server.id,
                    emby_user_id="disabled-notification",
                    username="disabled-notification",
                    portal_enabled=False,
                    playback_enabled=False,
                    expires_at=utcnow() - timedelta(minutes=1),
                )
                db.add_all([expired, reminder, disabled])
                self.fake.names[expired.emby_user_id] = expired.username
                self.fake.policies[expired.emby_user_id] = {
                    "IsDisabled": False,
                    "IsAdministrator": False,
                    "EnableMediaPlayback": True,
                }
                self.fake.names[disabled.emby_user_id] = disabled.username
                self.fake.policies[disabled.emby_user_id] = {
                    "IsDisabled": False,
                    "IsAdministrator": False,
                    "EnableMediaPlayback": False,
                }
                await db.commit()

                first = await services.process_expirations(db)
                # 模拟轮询间 Emby 被外部重新打开播放；已记录的回收事件仍不可重复通知。
                expired.playback_enabled = True
                # 外部重新启用到期账号时控制器会再次停用，但不重复通知。
                disabled.is_disabled = False
                await db.commit()
                second = await services.process_expirations(db)
                self.assertEqual(first["disabled"], 2)
                self.assertEqual(first["reminded"], 1)
                self.assertEqual(second, {"disabled": 0, "reminded": 0})
                self.assertEqual(
                    [category for _title, category in calls], ["expiry", "expiry", "expiry"]
                )
                self.assertFalse(expired.playback_enabled)
                self.assertIsNotNone(expired.playback_revoked_at)
                self.assertTrue(disabled.is_disabled)
                self.assertTrue(disabled.disabled_by_controller)
                self.assertIsNotNone(reminder.expiry_notified_at)

                recycled = ManagedUser(
                    server_id=server.id,
                    emby_user_id="recycled-notification",
                    username="recycled-notification",
                    self_registered=True,
                    portal_enabled=True,
                    activated_at=utcnow(),
                    expires_at=utcnow() - timedelta(days=2),
                    playback_revoked_at=utcnow() - timedelta(days=8),
                )
                db.add(recycled)
                self.fake.names[recycled.emby_user_id] = recycled.username
                self.fake.policies[recycled.emby_user_id] = {
                    "IsDisabled": False,
                    "IsAdministrator": False,
                    "EnableMediaPlayback": False,
                }
                await db.commit()
                self.assertEqual(await services.purge_expired_users(db), 1)
                self.assertIsNone(await db.get(ManagedUser, recycled.id))
                self.assertEqual(len(calls), 4)
                self.assertEqual(calls[-1][1], "expiry")
        finally:
            services.notify.push = original_push
            services.settings_store._cache = original_runtime


class MediaRequestPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.original_tmdb = services.TmdbClient
        self.original_push = services.notify.push
        services._tmdb_details_cache.clear()

    async def asyncTearDown(self):
        services.TmdbClient = self.original_tmdb
        services.notify.push = self.original_push
        await services.wait_background_tasks()

    async def test_confirm_commits_before_slow_poster_download(self):
        download_started = asyncio.Event()
        release_download = asyncio.Event()

        class BlockingTmdbClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def download_poster(self, _url):
                download_started.set()
                await release_download.wait()
                return b"poster", "image/jpeg"

        async def no_notification(*_args, **_kwargs):
            return None

        services.TmdbClient = BlockingTmdbClient
        services.notify.push = no_notification
        async with SessionLocal() as db:
            server = Server(
                name=f"poster-latency-{uuid4().hex}",
                base_url="http://mock",
                api_key_encrypted="test",
                enabled=True,
            )
            db.add(server)
            await db.flush()
            user = ManagedUser(
                server_id=server.id,
                emby_user_id="poster-user",
                username="poster-user",
                portal_enabled=True,
                portal_password_hash="test",
            )
            db.add(user)
            await db.flush()
            row = MediaRequest(
                server_id=server.id,
                managed_user_id=user.id,
                tmdb_id=9876,
                media_type="movie",
                title="慢海报测试",
                poster_url="https://image.tmdb.org/t/p/w342/test.jpg",
            )
            db.add(row)
            await db.commit()

            confirm_task = asyncio.create_task(
                services.confirm_media_request_group(
                    db,
                    server_id=server.id,
                    media_type="movie",
                    tmdb_id=9876,
                    confirmed_by="admin",
                )
            )
            try:
                await asyncio.wait_for(download_started.wait(), timeout=2)
                self.assertTrue(
                    confirm_task.done(),
                    "确认入库不应等待后台海报下载完成",
                )
                self.assertEqual(confirm_task.result(), 1)
            finally:
                release_download.set()
                if not confirm_task.done():
                    await asyncio.wait_for(confirm_task, timeout=2)
            await services.wait_background_tasks(timeout=2)
            await db.refresh(row)
            self.assertEqual(row.status, "in_library")
            self.assertEqual(
                Path(row.poster_local_path).as_posix(),
                f"posters/{server.id}-movie-9876.jpg",
            )
            self.assertTrue((services.get_settings().image_dir / row.poster_local_path).is_file())
