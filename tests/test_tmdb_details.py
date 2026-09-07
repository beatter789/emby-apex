import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app import services
from app.tmdb import TmdbClient


class TmdbDetailSerializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_details_appends_credits_and_limits_people(self):
        crew = [{"id": index, "name": f"导演{index}", "job": "Director"} for index in range(8)]
        cast = [{"id": index, "name": f"演员{index}", "character": f"角色{index}"} for index in range(20)]
        payload = {
            "id": 123,
            "title": "电影",
            "original_title": "Movie",
            "release_date": "2024-01-01",
            "tagline": "拯救世界",
            "status": "Released",
            "overview": "简介",
            "poster_path": "/poster.jpg",
            "backdrop_path": "/backdrop.jpg",
            "genres": [{"id": 1, "name": "动作"}],
            "vote_average": 7.5,
            "runtime": 110,
            "credits": {"crew": crew, "cast": cast},
        }
        client = object.__new__(TmdbClient)
        get = AsyncMock(return_value=payload)
        with patch.object(client, "_get", new=get):
            result = await client.details("movie", 123)
        get.assert_awaited_once_with("/movie/123", append_to_response="credits")
        self.assertEqual(result["backdrop_url"], "https://image.tmdb.org/t/p/w1280/backdrop.jpg")
        self.assertEqual(result["genres"], ["动作"])
        self.assertEqual(result["release_date"], "2024-01-01")
        self.assertEqual(result["tagline"], "拯救世界")
        self.assertEqual(result["status"], "Released")
        self.assertEqual(result["rating"], 7.5)
        self.assertEqual(result["runtime_minutes"], 110)
        self.assertNotIn("runtime", result)
        self.assertNotIn("vote_average", result)
        self.assertEqual(len(result["directors"]), 5)
        self.assertEqual(len(result["cast"]), 12)
        self.assertEqual(result["cast"][0]["character"], "角色0")

    async def test_tv_missing_fields_are_null_or_empty_lists(self):
        client = object.__new__(TmdbClient)
        with patch.object(
            client,
            "_get",
            new=AsyncMock(return_value={"id": 456, "name": "剧集", "credits": {}}),
        ):
            result = await client.details("tv", 456)
        self.assertIsNone(result["backdrop_url"])
        self.assertIsNone(result["overview"])
        self.assertIsNone(result["poster_url"])
        self.assertEqual(result["genres"], [])
        self.assertIsNone(result["release_date"])
        self.assertIsNone(result["tagline"])
        self.assertIsNone(result["status"])
        self.assertIsNone(result["rating"])
        self.assertIsNone(result["runtime_minutes"])
        self.assertEqual(result["directors"], [])
        self.assertEqual(result["cast"], [])
        self.assertIsNone(result["seasons"])
        self.assertIsNone(result["episodes"])

    async def test_tv_release_date_uses_first_air_date(self):
        client = object.__new__(TmdbClient)
        with patch.object(
            client,
            "_get",
            new=AsyncMock(
                return_value={
                    "id": 789,
                    "name": "剧集",
                    "first_air_date": "2023-07-08",
                    "vote_average": 8.2,
                    "episode_run_time": [45],
                    "number_of_seasons": 3,
                    "number_of_episodes": 24,
                    "credits": {},
                }
            ),
        ):
            result = await client.details("tv", 789)
        self.assertEqual(result["release_date"], "2023-07-08")
        self.assertEqual(result["rating"], 8.2)
        self.assertEqual(result["runtime_minutes"], 45)
        self.assertEqual(result["seasons"], 3)
        self.assertEqual(result["episodes"], 24)

    async def test_search_does_not_populate_full_detail_cache(self):
        original_client = services.TmdbClient
        services._tmdb_details_cache.clear()

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def search(self, *_args, **_kwargs):
                return [{
                    "tmdb_id": 99,
                    "media_type": "movie",
                    "title": "搜索结果",
                    "original_title": "Search",
                    "year": 2024,
                    "overview": "轻量",
                    "poster_url": "",
                }]

        services.TmdbClient = FakeClient
        try:
            result = await services.search_tmdb("movie", "搜索")
            self.assertEqual(len(result), 1)
            self.assertEqual(services._tmdb_details_cache, {})
        finally:
            services.TmdbClient = original_client
            services._tmdb_details_cache.clear()


if __name__ == "__main__":
    unittest.main()
