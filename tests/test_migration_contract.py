import unittest
from pathlib import Path


class MigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.admin_source = (cls.root / "app/routes.py").read_text(encoding="utf-8")
        cls.portal_source = (cls.root / "app/portal_routes.py").read_text(encoding="utf-8")

    def test_legacy_jinja_templates_and_renderers_are_removed(self):
        template_dir = self.root / "app/templates"
        self.assertFalse(any(template_dir.glob("*.html")))
        for source in (self.admin_source, self.portal_source):
            self.assertNotIn("Jinja2Templates", source)
            self.assertNotIn("TemplateResponse", source)
            self.assertNotIn("_render", source)

    def test_only_formal_shell_and_versioned_api_routes_remain(self):
        from app.main import app as admin_app
        from app.portal_main import app as portal_app

        admin_paths = {route.path for route in admin_app.routes}
        portal_paths = {route.path for route in portal_app.routes}
        self.assertIn("/dashboard", admin_paths)
        self.assertIn("/users", admin_paths)
        self.assertIn("/api/v1/dashboard", admin_paths)
        self.assertIn("/api/v1/settings/wecom/menu", admin_paths)
        self.assertIn("/account", portal_paths)
        self.assertIn("/requests", portal_paths)
        self.assertIn("/api/v1/account", portal_paths)
        self.assertIn("/api/v1/requests", portal_paths)
        for paths in (admin_paths, portal_paths):
            self.assertNotIn("/settings/wecom/menu", paths)
            self.assertNotIn("/settings/wecom/menu/sync", paths)
            self.assertNotIn("/settings/wecom/menu/delete", paths)

    def test_wechat_and_vue_static_assets_are_preserved(self):
        from app.main import app as admin_app
        from app.shell import admin_shell_router, portal_shell_router

        wechat = {
            (route.path, tuple(sorted(route.methods or ())))
            for route in admin_app.routes
            if route.path == "/wechat"
        }
        self.assertEqual(wechat, {('/wechat', ('GET',)), ('/wechat', ('POST',))})
        self.assertTrue(any(route.path == "/dashboard" for route in admin_shell_router.routes))
        self.assertTrue(any(route.path == "/account" for route in portal_shell_router.routes))
        self.assertTrue((self.root / "app/static/frontend/admin.html").is_file())
        self.assertTrue((self.root / "app/static/frontend/portal.html").is_file())
        self.assertTrue((self.root / "app/static/frontend/admin.js").is_file())
        self.assertTrue((self.root / "app/static/frontend/portal.js").is_file())


if __name__ == "__main__":
    unittest.main()
