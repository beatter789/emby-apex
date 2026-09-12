import unittest
from unittest.mock import AsyncMock

from app.moviepilot import MoviePilotClient


class MoviePilotEpisodeApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_episode_groups_accepts_native_list_and_wrapped_payload(self):
        client = object.__new__(MoviePilotClient)
        request = AsyncMock(side_effect=[
            [{"id": "group-1", "name": "Aired Order"}],
            {"results": [{"id": "group-2"}]},
        ])
        client._request = request

        self.assertEqual(await client.episode_groups(325709), [{"id": "group-1", "name": "Aired Order"}])
        self.assertEqual(await client.episode_groups("325709"), [{"id": "group-2"}])
        self.assertEqual(request.await_args_list[0].args, ("GET", "media/groups/325709"))
        self.assertEqual(request.await_args_list[1].args, ("GET", "media/groups/325709"))

    async def test_detail_qualifies_bare_provider_ids(self):
        client = object.__new__(MoviePilotClient)
        request = AsyncMock(return_value={"title": "剧集"})
        client._request = request

        self.assertEqual(await client.detail("tmdb", "325709", "电视剧"), {"title": "剧集"})
        self.assertEqual(request.await_args.kwargs, {"params": {"media_source": "themoviedb", "type_name": "电视剧"}})
        self.assertEqual(request.await_args.args, ("GET", "media/tmdb%3A325709"))

    async def test_group_seasons_and_season_episodes_use_v3_paths(self):
        client = object.__new__(MoviePilotClient)
        request = AsyncMock(side_effect=[
            [{"season_number": 1, "episode_count": 8}],
            {"episodes": [{"episode_number": 1, "name": "Pilot"}]},
        ])
        client._request = request

        self.assertEqual(await client.group_seasons("group/1"), [{"season_number": 1, "episode_count": 8}])
        self.assertEqual(
            await client.season_episodes(325709, 1, episode_group="group-1"),
            [{"episode_number": 1, "name": "Pilot"}],
        )
        self.assertEqual(request.await_args_list[0].args, ("GET", "media/group/seasons/group%2F1"))
        self.assertEqual(request.await_args_list[1].args, ("GET", "tmdb/325709/1"))
        self.assertEqual(request.await_args_list[1].kwargs, {"params": {"episode_group": "group-1"}})


if __name__ == "__main__":
    unittest.main()
