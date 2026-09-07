import os
import re
import unittest
from pathlib import Path


class ApexNamingTests(unittest.TestCase):
    def test_legacy_environment_names_are_ignored(self):
        from app.config import Settings

        old = {key: os.environ.get(key) for key in ("APEX_USER", "EC_ADMIN_USERNAME")}
        try:
            os.environ["APEX_USER"] = "apex-admin"
            os.environ["EC_ADMIN_USERNAME"] = "legacy-admin"
            self.assertEqual(Settings().admin_user, "apex-admin")
            os.environ.pop("APEX_USER")
            self.assertEqual(Settings().admin_user, "admin")
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_cookie_constants_and_api_shape(self):
        from app.main import app as admin_app
        from app.portal_main import app as portal_app
        from app.security import ADMIN_COOKIE_NAME, PORTAL_COOKIE_NAME

        self.assertEqual(ADMIN_COOKIE_NAME, "ec_session")
        self.assertEqual(PORTAL_COOKIE_NAME, "ec_portal")
        self.assertIn("/api/v1/health", {route.path for route in admin_app.routes})
        self.assertIn("/api/v1/health", {route.path for route in portal_app.routes})

    def test_frontend_skeleton_and_brand_cache(self):
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / "frontend/src/admin/index.html").exists())
        self.assertTrue((root / "frontend/src/portal/index.html").exists())
        self.assertTrue((root / "app/static/frontend/admin.html").exists())
        self.assertTrue((root / "app/static/frontend/portal.html").exists())
        sw = (root / "app/static/sw.js").read_text(encoding="utf-8")
        self.assertIn("emby-apex-ui-v12", sw)
        self.assertIn("mount-DSRKV7aU.js", sw)
        self.assertIn("assets/mount.css", sw)
        self.assertNotIn("format-BtXiRr21.js", sw)
        self.assertIn("isApi", sw)
        self.assertIn("isHtml", sw)
        self.assertIn("if (isApi || isHtml) return;", sw)
        admin_html = (root / "app/static/frontend/admin.html").read_text(encoding="utf-8")
        for chunk in re.findall(r"/static/frontend/chunks/[^\"']+\.js", admin_html):
            self.assertIn(chunk, sw)

    def test_admin_frontend_wecom_menu_and_mobile_row_actions(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "frontend/src/admin/AdminApp.vue").read_text(encoding="utf-8")
        styles = (root / "app/static/ui.css").read_text(encoding="utf-8")
        self.assertIn("/settings/wecom/menu/sync", source)
        self.assertIn("/settings/wecom/menu", source)
        self.assertIn("/settings/wecom/menu/delete", source)
        self.assertIn("class=\"list-item-actions\"", source)
        self.assertIn("#mobile-more-dialog.open", styles)
        self.assertIn("100dvh", styles)

    def test_admin_settings_notification_tabs_and_scoped_controls(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "frontend/src/admin/AdminApp.vue").read_text(encoding="utf-8")
        styles = (root / "app/static/ui.css").read_text(encoding="utf-8")
        built = (root / "app/static/frontend/admin.js").read_text(encoding="utf-8")
        for label in ("常规设置", "通知", "注册通知", "到期通知", "求片通知", "其他通知"):
            self.assertIn(label, source)
        self.assertIn("notification-light", source)
        self.assertIn("notification-channel-grid", styles)
        self.assertIn("notification-provider-card", source)
        self.assertIn("notification-config-dialog", source)
        self.assertIn("testNotificationChannel", source)
        self.assertIn("@click=\"testNotificationChannel\"", source)
        # The legacy settings branch is gated out of the compiled template;
        # connection testing is exposed from the channel detail dialog.
        self.assertNotIn("测试企业微信", built)
        self.assertIn("showsSharedChannelConfig", source)
        self.assertIn("const sharedNotificationFields = ['notify_proxy_url'];", source)
        self.assertIn("rowsFor(sharedNotificationFields)", source)
        self.assertNotIn("'notify_proxy_url'] },", source)
        self.assertIn("notification-shared-card", source)
        self.assertIn("渠道凭据在上方卡片中配置", source)
        self.assertIn("通知设置已保存", built)
        self.assertIn("测试 TMDB/代理", built)
        self.assertIn("/settings/wecom/menu/sync", built)
        # The new template gates the legacy settings branch out of the DOM.
        self.assertIn('currentPath !== \'/settings\'', source)

    def test_moviepilot_style_shared_vuetify_foundation(self):
        root = Path(__file__).resolve().parents[1]
        package = (root / "frontend/package.json").read_text(encoding="utf-8")
        vuetify = (root / "frontend/src/shared/vuetify/index.ts").read_text(encoding="utf-8")
        mount = (root / "frontend/src/shared/mount.ts").read_text(encoding="utf-8")
        shell = (root / "frontend/src/shared/layout/AppShell.vue").read_text(encoding="utf-8")
        admin_main = (root / "frontend/src/admin/main.ts").read_text(encoding="utf-8")
        portal_main = (root / "frontend/src/portal/main.ts").read_text(encoding="utf-8")
        admin = (root / "frontend/src/admin/LoginApp.vue").read_text(encoding="utf-8")
        portal = (root / "frontend/src/portal/AuthApp.vue").read_text(encoding="utf-8")
        notices = (root / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn('"vuetify"', package)
        self.assertIn("createVuetify", vuetify)
        self.assertIn("apexDark", vuetify)
        self.assertIn("v-app", shell)
        self.assertIn("apex-fade", shell)
        self.assertIn("mountApex", mount)
        self.assertIn("mountApex", admin_main)
        self.assertIn("mountApex", portal_main)
        for source in (admin, portal):
            self.assertIn("UiButton", source)
            self.assertIn("UiTextField", source)
            self.assertIn("UiFeedback", source)
        self.assertIn("MoviePilot v3", notices)
        self.assertIn("MIT License", notices)
        self.assertIn('"vue-router"', package)


if __name__ == "__main__":
    unittest.main()
