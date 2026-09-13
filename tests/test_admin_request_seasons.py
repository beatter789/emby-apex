import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from app import services
from app.api.admin import _request_group_row


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Db:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _stmt):
        return _Result(self.rows)


def _row(*, user_id: int, username: str, seasons: str, status: str = "pending"):
    request = SimpleNamespace(
        id=user_id,
        server_id=1,
        tmdb_id=99,
        media_type="tv",
        title="示例剧",
        original_title="Example",
        year=2024,
        overview="",
        poster_url="",
        poster_local_path="",
        poster_error="",
        status=status,
        note="",
        rejection_reason="",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        season_numbers=seasons,
    )
    return request, username, "主服务器"


class AdminRequestSeasonTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_and_each_user_keep_normalized_seasons(self):
        db = _Db(
            [
                _row(user_id=1, username="alice", seasons='[2, 0, "1", 1.5, true, -1]'),
                _row(user_id=2, username="bob", seasons='[3, 2, "bad"]'),
            ]
        )
        groups = await services.admin_media_request_groups(db, "pending")
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["season_numbers"], [0, 1, 2, 3])
        self.assertEqual(group["items"][0]["season_numbers"], [0, 1, 2])
        self.assertEqual(group["items"][1]["season_numbers"], [2, 3])

        payload = _request_group_row(group)
        self.assertEqual(payload["season_numbers"], [0, 1, 2, 3])
        self.assertEqual(payload["items"][0]["season_numbers"], [0, 1, 2])
        self.assertEqual(payload["items"][1]["season_numbers"], [2, 3])
        json.dumps(payload)  # response must remain JSON serializable

    async def test_malformed_or_empty_legacy_values_are_safe(self):
        db = _Db([_row(user_id=1, username="legacy", seasons="not-json")])
        groups = await services.admin_media_request_groups(db, "pending")
        self.assertEqual(groups[0]["season_numbers"], [])
        self.assertEqual(groups[0]["items"][0]["season_numbers"], [])


if __name__ == "__main__":
    unittest.main()
