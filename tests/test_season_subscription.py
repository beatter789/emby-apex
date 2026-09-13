import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import services


class _Db:
    def __init__(self, row):
        self.row = row
        self.commits = 0
        self.rollbacks = 0

    async def get(self, _model, _id):
        return self.row

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class SeasonSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_adds_and_pauses_moviepilot_subscription_and_merges_ids(self):
        row = SimpleNamespace(
            id=7,
            server_id=3,
            managed_user_id=11,
            status="pending",
            media_type="tv",
            title="测试剧",
            media_source="themoviedb",
            media_id="99",
            tmdb_id=99,
            year=2024,
            season_numbers="[1]",
            moviepilot_subscribe_ids="[501]",
            moviepilot_subscribe_state="S",
            moviepilot_error="",
        )
        db = _Db(row)
        user = SimpleNamespace(id=11, server_id=3)

        class Client:
            subscribe = AsyncMock(return_value=502)
            pause = AsyncMock()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        client = Client()
        with patch.object(services, "moviepilot_enabled", return_value=True), patch.object(
            services, "MoviePilotClient", return_value=client
        ):
            result = await services.add_media_request_season(db, user, 7, 2)

        self.assertIs(result, row)
        self.assertEqual(json.loads(row.season_numbers), [1, 2])
        self.assertEqual(json.loads(row.moviepilot_subscribe_ids), [501, 502])
        client.subscribe.assert_awaited_once()
        client.pause.assert_awaited_once_with(502)
        self.assertEqual(db.commits, 1)

    async def test_repeated_season_is_idempotent_without_remote_call(self):
        row = SimpleNamespace(
            id=7,
            server_id=3,
            managed_user_id=11,
            status="pending",
            media_type="tv",
            season_numbers="[2]",
            moviepilot_subscribe_ids="[]",
        )
        db = _Db(row)
        user = SimpleNamespace(id=11, server_id=3)
        with patch.object(services, "moviepilot_enabled", return_value=True), patch.object(
            services, "MoviePilotClient"
        ) as factory:
            result = await services.add_media_request_season(db, user, 7, 2)
        self.assertIs(result, row)
        factory.assert_not_called()
        self.assertEqual(db.commits, 0)


if __name__ == "__main__":
    unittest.main()
