import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import notify, services, settings_store
from app.config import get_settings
from app.db import SessionLocal, engine, init_db
from app.main import app as admin_app
from app.models import Base, BillCodeUsage, ManagedUser, RedeemCode, Server
from app.portal_main import app as portal_app
from app.security import hash_password


@asynccontextmanager
async def _test_lifespan(_app):
    yield


def _bill_code(length: int = 30) -> str:
    today = datetime.now(ZoneInfo(get_settings().timezone)).strftime("%Y%m%d")
    return "1000" + "123456" + today + ("9" * (length - 18))


class ActivationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())
        cls.old_admin = admin_app.router.lifespan_context
        cls.old_portal = portal_app.router.lifespan_context
        admin_app.router.lifespan_context = _test_lifespan
        portal_app.router.lifespan_context = _test_lifespan

    @classmethod
    def tearDownClass(cls):
        admin_app.router.lifespan_context = cls.old_admin
        portal_app.router.lifespan_context = cls.old_portal

    def setUp(self):
        asyncio.run(self._reset())
        self.portal = TestClient(portal_app)
        self.admin = TestClient(admin_app)
        self.portal.__enter__()
        self.admin.__enter__()

    def tearDown(self):
        self.portal.__exit__(None, None, None)
        self.admin.__exit__(None, None, None)

    async def _reset(self):
        async with engine.begin() as conn:
            for table in reversed(list(Base.metadata.sorted_tables)):
                await conn.execute(table.delete())
        async with SessionLocal() as db:
            server = Server(name="activation", base_url="http://activation", api_key_encrypted="x")
            db.add(server)
            await db.flush()
            db.add(
                ManagedUser(
                    server_id=server.id,
                    emby_user_id="activation-user",
                    username="activation-user",
                    portal_enabled=True,
                    portal_password_hash=hash_password("password"),
                )
            )
            await db.commit()

    def _portal_login(self):
        token = self.portal.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
        response = self.portal.post(
            "/api/v1/auth/login",
            json={"username": "activation-user", "password": "password", "csrf_token": token},
        )
        assert response.status_code == 200
        return self.portal.get("/api/v1/account").json()["data"]["csrf_token"]

    def _admin_login(self):
        token = self.admin.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
        response = self.admin.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "admin", "csrf_token": token},
        )
        assert response.status_code == 200
        return self.admin.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]

    def test_bill_code_rule_accepts_30_31_32_and_rejects_bad_inputs(self):
        for length in (30, 31, 32):
            assert services.validate_bill_code(_bill_code(length))
        invalid = [
            "2000" + _bill_code()[4:],
            "10000" + _bill_code()[5:],
            _bill_code()[:-1],
            _bill_code() + "000",
            _bill_code()[:10] + "0" + _bill_code()[10:-1],
            _bill_code()[:10] + "20230230" + _bill_code()[18:],
            _bill_code()[:10] + "20200101" + _bill_code()[18:],
            _bill_code()[:10] + "2026010A" + _bill_code()[18:],
            _bill_code()[:10] + "20260101" + _bill_code()[18:-1] + " ",
        ]
        for value in invalid:
            try:
                services.validate_bill_code(value)
            except services.RegistrationError:
                pass
            else:
                raise AssertionError(value)

    def test_bill_code_generates_user_owned_pending_code_and_daily_limit(self):
        csrf = self._portal_login()
        for index in range(3):
            response = self.portal.post(
                "/api/v1/account/activation/bill-code",
                json={"bill_code": _bill_code()[:-1] + str(index), "csrf_token": csrf},
            )
            assert response.status_code == 201, response.text
            assert response.json()["data"]["expires_at"] is None
        fourth = self.portal.post(
            "/api/v1/account/activation/bill-code",
            json={"bill_code": _bill_code()[:-1] + "8", "csrf_token": csrf},
        )
        assert fourth.status_code == 400
        activation = self.portal.get("/api/v1/account/activation")
        assert activation.status_code == 200
        assert activation.json()["data"]["used_today"] == 3
        assert len(activation.json()["data"]["pending_codes"]) == 3

        async def check_owner():
            async with SessionLocal() as db:
                rows = (await db.scalars(select(RedeemCode))).all()
                assert all(row.owner_user_id is not None for row in rows)

        asyncio.run(check_owner())

    def test_bill_code_success_emits_activation_event_after_commit(self):
        csrf = self._portal_login()
        bill = _bill_code()
        with patch.object(
            services.notify, "emit_activation_success", new=AsyncMock()
        ) as emit:
            response = self.portal.post(
                "/api/v1/account/activation/bill-code",
                json={"bill_code": bill, "csrf_token": csrf},
            )
        self.assertEqual(response.status_code, 201, response.text)
        emit.assert_awaited_once()
        event = emit.await_args.args[0]
        self.assertEqual(event["username"], "activation-user")
        self.assertEqual(event["bill_code"], bill)
        self.assertRegex(event["activated_at"], r"T\d{2}:\d{2}:\d{2}")
        self.assertEqual(event["redeem_code"], response.json()["data"]["redeem_code"])

        async def check_committed():
            async with SessionLocal() as db:
                self.assertIsNotNone(
                    await db.scalar(select(RedeemCode).where(RedeemCode.source_bill_code == bill))
                )

        asyncio.run(check_committed())

    def test_bill_code_notification_success_is_persistently_idempotent(self):
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
        bill = _bill_code()
        try:
            with patch.object(notify, "_send_wecom", new=AsyncMock()) as sender:
                response = self.portal.post(
                    "/api/v1/account/activation/bill-code",
                    json={"bill_code": bill, "csrf_token": self._portal_login()},
                )
                self.assertEqual(response.status_code, 201, response.text)
                sender.assert_awaited_once()

            async def check_marker():
                async with SessionLocal() as db:
                    usage = await db.scalar(
                        select(BillCodeUsage).where(BillCodeUsage.bill_code == bill)
                    )
                    self.assertIsNotNone(usage)
                    self.assertIsNotNone(usage.activation_notification_sent_at)

            asyncio.run(check_marker())
            notify._activation_notified_bill_codes.clear()
            with patch.object(notify, "_send_wecom", new=AsyncMock()) as sender:
                result = asyncio.run(
                    notify.activation_success(
                        {
                            "username": "activation-user",
                            "activated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "bill_code": bill,
                            "redeem_code": response.json()["data"]["redeem_code"],
                        }
                    )
                )
                self.assertTrue(result)
                sender.assert_not_awaited()
        finally:
            notify._activation_notified_bill_codes.clear()
            settings_store._cache = original

    def test_concurrent_submissions_never_exceed_three_successes(self):
        self._portal_login()

        async def run():
            async with SessionLocal() as db:
                user_id = (await db.scalar(select(ManagedUser.id))).__int__()

            async def submit(index: int):
                async with SessionLocal() as db:
                    user = await db.get(ManagedUser, user_id)
                    try:
                        await services.create_redeem_code_from_bill(db, user, _bill_code()[:-2] + f"{index:02d}")
                        return True
                    except services.RegistrationError:
                        return False

            return await asyncio.gather(*(submit(index) for index in range(5)))

        results = asyncio.run(run())
        assert sum(results) <= 3

    def test_duplicate_bill_code_and_redeem_adds_thirty_days(self):
        csrf = self._portal_login()
        bill = _bill_code()
        first = self.portal.post(
            "/api/v1/account/activation/bill-code",
            json={"bill_code": bill, "csrf_token": csrf},
        )
        assert first.status_code == 201
        duplicate = self.portal.post(
            "/api/v1/account/activation/bill-code",
            json={"bill_code": bill, "csrf_token": csrf},
        )
        assert duplicate.status_code == 400
        pending = self.portal.get("/api/v1/account/activation").json()["data"]["pending_codes"]
        code = pending[0]["code"]

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def set_user_disabled(self, *_args):
                return None

            async def set_playback_allowed(self, *_args):
                return None

        with patch("app.services.client_for", return_value=FakeClient()):
            redeemed = self.portal.post(
                "/api/v1/account/redeem",
                json={"code": code, "csrf_token": csrf},
            )
        assert redeemed.status_code == 200, redeemed.text

        admin_token = self._admin_login()
        listed = self.admin.get("/api/v1/codes")
        assert listed.status_code == 200
        assert listed.json()["data"]["codes"][0]["source"] == "user"
        assert listed.json()["data"]["codes"][0]["owner_user_id"] is not None
        assert redeemed.json()["data"]["duration"] == "30 天"
        account_expiry = datetime.fromisoformat(
            redeemed.json()["data"]["account"]["expires_at"]
        )
        assert 29 <= (account_expiry - datetime.now(timezone.utc)).days <= 30

    def test_admin_cannot_delete_pending_user_generated_code(self):
        csrf = self._portal_login()
        response = self.portal.post(
            "/api/v1/account/activation/bill-code",
            json={"bill_code": _bill_code(), "csrf_token": csrf},
        )
        assert response.status_code == 201
        code = response.json()["data"]["redeem_code"]
        admin_csrf = self._admin_login()
        listed = self.admin.get("/api/v1/codes").json()["data"]["codes"]
        row = next(item for item in listed if item["code"] == code)
        deleted = self.admin.request(
            "DELETE",
            f"/api/v1/codes/{row['id']}",
            json={"csrf_token": admin_csrf},
        )
        assert deleted.status_code == 400

    def test_activation_image_requires_admin_csrf_and_safe_file(self):
        response = self.admin.get("/api/v1/settings/activation-image")
        assert response.status_code == 401
        csrf = self._admin_login()
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        missing_csrf = self.admin.post(
            "/api/v1/settings/activation-image", files={"file": ("scan.png", png, "image/png")}
        )
        assert missing_csrf.status_code == 400
        uploaded = self.admin.post(
            "/api/v1/settings/activation-image",
            data={"csrf_token": csrf},
            files={"file": ("scan.png", png, "image/png")},
        )
        assert uploaded.status_code == 200, uploaded.text
        metadata = self.admin.get("/api/v1/settings/activation-image")
        assert metadata.status_code == 200
        assert metadata.json()["data"]["mime"] == "image/png"
        self._portal_login()
        activation = self.portal.get("/api/v1/account/activation").json()["data"]
        assert activation["scan_image_url"] == "/api/v1/account/activation-image"
        assert self.portal.get(activation["scan_image_url"]).status_code == 200
        bad = self.admin.post(
            "/api/v1/settings/activation-image",
            data={"csrf_token": csrf},
            files={"file": ("payload.exe", png, "image/png")},
        )
        assert bad.status_code == 400
        traversal = self.admin.post(
            "/api/v1/settings/activation-image",
            data={"csrf_token": csrf},
            files={"file": ("..\\evil.png", png, "image/png")},
        )
        assert traversal.status_code == 400
        missing_clear_csrf = self.admin.request("DELETE", "/api/v1/settings/activation-image", json={})
        assert missing_clear_csrf.status_code == 400
        cleared = self.admin.request(
            "DELETE",
            "/api/v1/settings/activation-image",
            json={"csrf_token": csrf},
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["data"]["scan_image_url"] == ""
        assert self.admin.get("/api/v1/settings/activation-image").json()["data"]["scan_image_url"] == ""

    def test_sixth_wrong_redeem_attempt_locks_account(self):
        csrf = self._portal_login()
        for index in range(5):
            response = self.portal.post(
                "/api/v1/account/redeem",
                json={"code": f"WRONG-{index}", "csrf_token": csrf},
            )
            assert response.status_code == 400
            assert "停用" not in response.json()["error"]
        locked = self.portal.post(
            "/api/v1/account/redeem",
            json={"code": "WRONG-LOCK", "csrf_token": csrf},
        )
        assert locked.status_code == 400
        assert "停用" in locked.json()["error"]
        async def check_disabled():
            async with SessionLocal() as db:
                user = await db.scalar(select(ManagedUser).where(ManagedUser.username == "activation-user"))
                assert user.activation_locked is True
                assert user.is_disabled is True
        asyncio.run(check_disabled())

    def test_bill_code_failures_are_generic_and_fifth_attempt_locks(self):
        csrf = self._portal_login()
        invalid = [
            "",
            "10000" + "1" * 25,
            _bill_code()[:10] + "20200101" + _bill_code()[18:],
            "letters-only",
            "still-invalid",
            "after-lock",
        ]
        messages = []
        for index, value in enumerate(invalid):
            response = self.portal.post(
                "/api/v1/account/activation/bill-code",
                json={"bill_code": value, "csrf_token": csrf},
            )
            self.assertEqual(response.status_code, 400)
            messages.append(response.json()["error"])
            if index < 5:
                self.assertEqual(messages[-1], "账单码无效，请检查后重试")
            else:
                self.assertEqual(messages[-1], "账户已停用，账单码激活功能不可用")
        self.assertEqual(messages[0], messages[1])

        async def check_locked():
            async with SessionLocal() as db:
                user = await db.scalar(
                    select(ManagedUser).where(ManagedUser.username == "activation-user")
                )
                self.assertEqual(user.activation_failed_attempts, 5)
                self.assertTrue(user.activation_locked)
                self.assertTrue(user.is_disabled)
                self.assertIsNotNone(user.activation_locked_at)

        asyncio.run(check_locked())

    def test_concurrent_bill_code_failures_cap_at_five(self):
        self._portal_login()

        async def run():
            async with SessionLocal() as db:
                user_id = int(await db.scalar(select(ManagedUser.id)))

            async def submit(index: int):
                async with SessionLocal() as db:
                    user = await db.get(ManagedUser, user_id)
                    try:
                        await services.create_redeem_code_from_bill(db, user, f"BAD-{index}")
                    except services.RegistrationError as exc:
                        return str(exc)
                    return "unexpected-success"

            results = await asyncio.gather(*(submit(index) for index in range(20)))
            async with SessionLocal() as db:
                user = await db.get(ManagedUser, user_id)
                return results, user.activation_failed_attempts, user.activation_locked

        results, attempts, locked = asyncio.run(run())
        self.assertEqual(attempts, 5)
        self.assertTrue(locked)
        self.assertNotIn("unexpected-success", results)

    def test_admin_reenable_clears_bill_code_lock(self):
        self.test_bill_code_failures_are_generic_and_fifth_attempt_locks()

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def set_user_disabled(self, *_args):
                return None

            async def list_sessions(self):
                return []

        with patch("app.services.client_for", return_value=FakeClient()):
            admin_csrf = self._admin_login()
            users = self.admin.get("/api/v1/users").json()["data"]["users"]
            user_id = next(row["id"] for row in users if row["username"] == "activation-user")
            response = self.admin.patch(
                f"/api/v1/users/{user_id}",
                json={"disabled": False, "csrf_token": admin_csrf},
            )
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()["data"]
        self.assertFalse(row["activation_locked"])
        self.assertEqual(row["activation_failed_attempts"], 0)
        self.assertIsNone(row["activation_locked_at"])
        self.assertFalse(row["is_disabled"])

    def test_admin_user_is_exempt_from_bill_code_failure_lock(self):
        async def promote_to_admin():
            async with SessionLocal() as db:
                user = await db.scalar(
                    select(ManagedUser).where(ManagedUser.username == "activation-user")
                )
                user.is_admin = True
                await db.commit()

        asyncio.run(promote_to_admin())
        csrf = self._portal_login()
        for index in range(6):
            response = self.portal.post(
                "/api/v1/account/activation/bill-code",
                json={"bill_code": f"ADMIN-BAD-{index}", "csrf_token": csrf},
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"], "账单码无效，请检查后重试")

        async def check_exempt():
            async with SessionLocal() as db:
                user = await db.scalar(
                    select(ManagedUser).where(ManagedUser.username == "activation-user")
                )
                self.assertTrue(user.is_admin)
                self.assertEqual(user.activation_failed_attempts, 0)
                self.assertFalse(user.activation_locked)
                self.assertFalse(user.is_disabled)

        asyncio.run(check_exempt())
