import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import services


class MoviePilotRejectCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_deletes_only_paused_apex_subscriptions(self):
        class FakeClient:
            def __init__(self):
                self.delete_subscription = AsyncMock()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        client = FakeClient()
        rows = [
            SimpleNamespace(
                id=1,
                moviepilot_subscribe_state="S",
                moviepilot_subscribe_ids="[101, 102, 101, 0, \"bad\"]",
            ),
            SimpleNamespace(
                id=2,
                moviepilot_subscribe_state="",
                moviepilot_subscribe_ids="[103]",
            ),
        ]
        with patch.object(services, "moviepilot_enabled", return_value=True), patch.object(
            services, "MoviePilotClient", return_value=client
        ):
            result = await services._delete_paused_moviepilot_subscriptions(rows)

        self.assertEqual(client.delete_subscription.await_args_list[0].args, (101,))
        self.assertEqual(client.delete_subscription.await_args_list[1].args, (102,))
        self.assertEqual(result[1][0], {101, 102})
        self.assertEqual(result[1][1], [])
        self.assertNotIn(2, result)

    async def test_cleanup_continues_and_reports_remote_delete_failure(self):
        class FakeClient:
            def __init__(self):
                self.delete_subscription = AsyncMock(
                    side_effect=[RuntimeError("network down"), None]
                )

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        client = FakeClient()
        row = SimpleNamespace(
            id=3,
            moviepilot_subscribe_state="S",
            moviepilot_subscribe_ids="[201, 202]",
        )
        with patch.object(services, "moviepilot_enabled", return_value=True), patch.object(
            services, "MoviePilotClient", return_value=client
        ):
            result = await services._delete_paused_moviepilot_subscriptions([row])

        self.assertEqual(result[3][0], {202})
        self.assertEqual(len(result[3][1]), 1)
        self.assertIn("201", result[3][1][0])


if __name__ == "__main__":
    unittest.main()
