import asyncio
import unittest
from pathlib import Path

from app import scheduler, services
from app.main import app
from app.models import Server
from app.webhook import emby_webhook
from starlette.requests import Request


class _PlaybackDb:
    def __init__(self):
        self.rows = []
        self.commits = 0
        self.new = []

    def add(self, value):
        self.rows.append(value)

    async def scalar(self, _statement):
        return None

    async def commit(self):
        self.commits += 1


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        services._playback_cache.clear()
        services._playback_generations.clear()
        scheduler.live_cache = []

    async def asyncTearDown(self):
        services._playback_cache.clear()
        services._playback_generations.clear()
        scheduler.live_cache = []

    def test_route_is_admin_only(self):
        from app.portal_main import app as portal_app

        admin_paths = {route.path for route in app.routes}
        portal_paths = {route.path for route in portal_app.routes}
        self.assertIn("/webhook", admin_paths)
        self.assertNotIn("/webhook", portal_paths)

    def test_dockerfile_does_not_run_periodic_health_probe(self):
        dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
        self.assertNotIn("HEALTHCHECK", dockerfile.read_text(encoding="utf-8"))

    def test_scheduler_has_no_stale_playback_job(self):
        scheduler_source = Path(__file__).resolve().parents[1] / "app/scheduler.py"
        source = scheduler_source.read_text(encoding="utf-8")
        self.assertNotIn("stale_playback", source)
        self.assertNotIn("_stale_playback_job", source)

    async def test_invalid_token_is_rejected_before_payload_or_database_access(self):
        body = b'{"Event":"PlaybackStart"}'

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "path": "/webhook",
                "raw_path": b"/webhook",
                "query_string": b"token=wrong",
                "headers": [],
                "client": ("127.0.0.1", 1),
                "server": ("test", 8000),
                "scheme": "http",
            },
            receive,
        )
        response = await emby_webhook(request, None)
        self.assertEqual(response.status_code, 401)

    async def test_valid_test_event_is_visible_at_info_without_payload_logging(self):
        body = b'{"Event":"Test","secret":"raw-secret"}'

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "path": "/webhook",
                "raw_path": b"/webhook",
                "query_string": b"token=embyapex",
                "headers": [],
                "client": ("127.0.0.1", 1),
                "server": ("test", 8000),
                "scheme": "http",
            },
            receive,
        )
        with self.assertLogs("app.webhook", level="INFO") as captured:
            response = await emby_webhook(request, None)
        self.assertEqual(response.status_code, 200)
        self.assertIn("事件已忽略", "\n".join(captured.output))
        self.assertNotIn("raw-secret", "\n".join(captured.output))

    def test_event_classifier_and_payload_normalization(self):
        payload = {
            "Event": "PlaybackStart",
            "User": {"Id": "u1", "Name": "alice"},
            "Session": {
                "Id": "s1",
                "Client": "iOS",
                "DeviceName": "Phone",
                "DeviceId": "d1",
            },
            "Item": {"Id": "m1", "Name": "Movie", "Type": "Movie", "RunTimeTicks": 100},
            "PlaybackInfo": {"PositionTicks": 10, "PlayMethod": "DirectPlay"},
        }
        self.assertEqual(services.webhook_event_kind(payload), "start")
        normalized = services.normalize_webhook_session(payload, kind="start")
        self.assertEqual(normalized["Id"], "s1")
        self.assertEqual(normalized["UserId"], "u1")
        self.assertEqual(normalized["NowPlayingItem"]["Id"], "m1")
        self.assertEqual(normalized["PlayState"]["PositionTicks"], 10)
        self.assertEqual(services.webhook_event_kind({"Event": "PlaybackPause"}), "pause")
        self.assertEqual(services.webhook_event_kind({"Event": "PlaybackUnpause"}), "resume")
        self.assertEqual(services.webhook_event_kind({"Event": "PlaybackStop"}), "stop")

    async def test_start_progress_and_stop_flush_once(self):
        db = _PlaybackDb()
        server = Server(id=1, name="main", base_url="http://emby.local", api_key_encrypted="x")
        start = {
            "Event": "playback.start",
            "User": {"Id": "u1", "Name": "alice"},
            "Session": {"Id": "s1", "Client": "Web", "DeviceName": "Browser"},
            "Item": {"Id": "m1", "Name": "Movie", "Type": "Movie", "RunTimeTicks": 10000000},
            "PlaybackInfo": {"PositionTicks": 0, "PlayMethod": "DirectPlay"},
        }
        result = await services.process_webhook_event(db, server, start)
        self.assertEqual(result["kind"], "start")
        self.assertEqual(len(db.rows), 0)
        progress = dict(start)
        progress["Event"] = "PlaybackProgress"
        progress["PlaybackInfo"] = {"PositionTicks": 5000000, "PlayMethod": "DirectPlay"}
        await services.process_webhook_event(db, server, progress)
        self.assertEqual(len(db.rows), 0)
        pause = dict(progress)
        pause["Event"] = "PlaybackPause"
        await services.process_webhook_event(db, server, pause)
        resume = dict(progress)
        resume["Event"] = "PlaybackUnpause"
        await services.process_webhook_event(db, server, resume)
        self.assertEqual(len(db.rows), 0)
        self.assertEqual(db.commits, 0)
        stop = dict(start)
        stop["Event"] = "PlaybackStop"
        result = await services.process_webhook_event(db, server, stop)
        self.assertEqual(result["kind"], "stop")
        self.assertEqual(len(db.rows), 1)
        self.assertEqual(db.commits, 1)


if __name__ == "__main__":
    unittest.main()
