"""TMDB API 客户端。API Key 只在服务端读取运行期设置。"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from . import settings_store
from .config import get_settings
from .logging_config import register_sensitive_values

TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMAGE = "https://image.tmdb.org/t/p/w342"
TMDB_BACKDROP = "https://image.tmdb.org/t/p/w1280"

logger = logging.getLogger(__name__)


class TmdbError(RuntimeError):
    pass


def _safe_error(value: object, *, proxy: str = "", key: str = "") -> str:
    """日志只保留诊断信息，绝不把代理凭据或 API Key 写入日志。"""
    text = str(value)
    if key:
        text = text.replace(key, "<tmdb-key>")
    if proxy:
        text = text.replace(proxy, "<tmdb-proxy>")
    return text


def _validate_proxy(value: str) -> str:
    proxy = value.strip()
    if not proxy:
        return ""
    try:
        parsed = httpx.URL(proxy)
    except Exception as exc:
        raise TmdbError("TMDB 代理地址无效，请在管理端设置中检查") from exc
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.host:
        raise TmdbError("TMDB 代理地址无效，请在管理端设置中检查")
    return proxy


def _year(value: Any) -> int | None:
    if value is None:
        return None
    try:
        text = str(value)
        return int(text[:4]) if len(text) >= 4 else None
    except (TypeError, ValueError):
        return None


def _title(item: dict[str, Any], media_type: str) -> tuple[str, str]:
    if media_type == "movie":
        return str(item.get("title") or item.get("original_title") or ""), str(
            item.get("original_title") or item.get("title") or ""
        )
    return str(item.get("name") or item.get("original_name") or ""), str(
        item.get("original_name") or item.get("name") or ""
    )


def normalize_result(item: dict[str, Any], media_type: str | None = None) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    kind = media_type or str(item.get("media_type") or "")
    if kind not in {"movie", "tv"}:
        return None
    title, original = _title(item, kind)
    date_value = item.get("release_date") if kind == "movie" else item.get("first_air_date")
    poster_path = str(item.get("poster_path") or "")
    parsed_id = _optional_int(item.get("id"))
    return {
        "tmdb_id": parsed_id or 0,
        "media_type": kind,
        "title": title,
        "original_title": original,
        "year": _year(date_value),
        "overview": str(item.get("overview") or ""),
        "poster_url": f"{TMDB_IMAGE}{poster_path}" if poster_path else "",
    }


def _optional_int(value: Any) -> int | None:
    """Convert TMDB numeric fields while treating missing/invalid values as null."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _credit_person(item: Any, *, with_character: bool = False) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    person: dict[str, Any] = {"id": _optional_int(item.get("id")), "name": name}
    if with_character:
        person["character"] = str(item.get("character") or "").strip() or None
    profile_path = str(item.get("profile_path") or "").strip()
    person["profile_url"] = f"{TMDB_IMAGE}{profile_path}" if profile_path else None
    return person


def normalize_details(item: dict[str, Any], media_type: str) -> dict[str, Any] | None:
    """Serialize a full movie/TV response without leaking the raw TMDB payload."""
    if not isinstance(item, dict):
        return None
    normalized = normalize_result(item, media_type)
    if normalized is None or not normalized["tmdb_id"]:
        return None

    backdrop_path = str(item.get("backdrop_path") or "").strip()
    raw_genres = item.get("genres") if isinstance(item.get("genres"), list) else []
    genres = [
        str(entry.get("name") or "").strip()
        for entry in raw_genres
        if isinstance(entry, dict) and str(entry.get("name") or "").strip()
    ]
    credits = item.get("credits") if isinstance(item.get("credits"), dict) else {}
    raw_crew = credits.get("crew") if isinstance(credits.get("crew"), list) else []
    raw_cast = credits.get("cast") if isinstance(credits.get("cast"), list) else []
    directors: list[dict[str, Any]] = []
    for entry in raw_crew:
        if not isinstance(entry, dict) or str(entry.get("job") or "").casefold() != "director":
            continue
        person = _credit_person(entry)
        if person:
            directors.append(person)
        if len(directors) >= 5:
            break
    cast: list[dict[str, Any]] = []
    for entry in raw_cast:
        person = _credit_person(entry, with_character=True)
        if person:
            cast.append(person)
        if len(cast) >= 12:
            break

    raw_episode_run_time = item.get("episode_run_time") if isinstance(item.get("episode_run_time"), list) else []
    episode_run_time = [
        value
        for value in (_optional_int(raw) for raw in raw_episode_run_time)
        if value is not None and value > 0
    ]
    runtime = _optional_int(item.get("runtime"))
    if runtime is None and media_type == "tv" and episode_run_time:
        runtime = episode_run_time[0]
    seasons = _optional_int(item.get("number_of_seasons"))
    episodes = _optional_int(item.get("number_of_episodes"))
    release_value = item.get("release_date") if media_type == "movie" else item.get("first_air_date")
    release_date = str(release_value or "").strip() or None
    normalized.update(
        {
            # Detail responses use null for absent scalar metadata.  Search
            # responses intentionally keep their lightweight string shape.
            "overview": str(item.get("overview") or "").strip() or None,
            "poster_url": normalized.get("poster_url") or None,
            "original_title": normalized.get("original_title") or None,
            "release_date": release_date,
            "tagline": str(item.get("tagline") or "").strip() or None,
            "status": str(item.get("status") or "").strip() or None,
            "backdrop_url": f"{TMDB_BACKDROP}{backdrop_path}" if backdrop_path else None,
            "genres": genres,
            "rating": _optional_float(item.get("vote_average")),
            "runtime_minutes": runtime,
            "directors": directors,
            "cast": cast,
            "seasons": seasons,
            "episodes": episodes,
        }
    )
    return normalized


class TmdbClient:
    def __init__(
        self,
        *,
        timeout: float | None = None,
        api_key: str | None = None,
        proxy_url: str | None = None,
    ) -> None:
        runtime = settings_store.current()
        key = (runtime.tmdb_api_key if api_key is None else api_key).strip()
        if not key:
            raise TmdbError("管理员尚未配置 TMDB API Key")
        self._timeout = timeout or get_settings().http_timeout_seconds
        self._key = key
        self._proxy = _validate_proxy(runtime.tmdb_proxy_url if proxy_url is None else proxy_url)
        register_sensitive_values(self._key, self._proxy)

    async def __aenter__(self) -> "TmdbClient":
        self._client = httpx.AsyncClient(
            base_url=TMDB_API,
            timeout=self._timeout,
            headers={"Accept": "application/json"},
            proxy=self._proxy or None,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        client = getattr(self, "_client", None)
        if client is not None:
            await client.aclose()
            self._client = None

    async def _get(self, path: str, **params: Any) -> Any:
        client = getattr(self, "_client", None)
        if client is None:
            raise TmdbError("TmdbClient 必须在 async with 上下文中使用")
        params.update({"api_key": self._key, "language": "zh-CN"})
        try:
            response = await client.get(path, params=params)
        except httpx.TimeoutException as exc:
            logger.warning("TMDB 请求超时（%s）：%s", path, _safe_error(exc, proxy=self._proxy, key=self._key))
            raise TmdbError("连接 TMDB 失败，请检查网络或代理设置") from exc
        except httpx.HTTPError as exc:
            logger.warning("TMDB 连接失败（%s）：%s", path, _safe_error(exc, proxy=self._proxy, key=self._key))
            raise TmdbError("连接 TMDB 失败，请检查网络或代理设置") from exc
        if response.status_code >= 400:
            logger.warning("TMDB 返回错误（%s）：HTTP %s", path, response.status_code)
            payload: dict[str, Any] = {}
            try:
                body = response.json()
                if isinstance(body, dict):
                    payload = body
            except ValueError:
                pass
            code = payload.get("status_code")
            if response.status_code in {401, 403} or code in {7, 16}:
                raise TmdbError("TMDB API Key 无效，请在管理端设置中检查")
            if response.status_code == 429:
                raise TmdbError("TMDB 请求过于频繁，请稍后重试")
            if response.status_code >= 500:
                raise TmdbError("TMDB 服务暂时不可用，请稍后重试")
            raise TmdbError(f"TMDB 请求失败（HTTP {response.status_code}）")
        try:
            return response.json()
        except ValueError as exc:
            raise TmdbError("TMDB 返回格式不正确") from exc

    async def search(self, mode: str, query: str, year: int | None = None) -> list[dict[str, Any]]:
        if mode == "multi":
            data = await self._get("/search/multi", query=query, page=1)
        elif mode in {"movie", "tv"}:
            data = await self._get(f"/search/{mode}", query=query, page=1)
        else:
            raise TmdbError("无效的 TMDB 搜索模式")
        results: list[dict[str, Any]] = []
        for item in list((data or {}).get("results") or [])[:20]:
            normalized = normalize_result(item, None if mode == "multi" else mode)
            if normalized and normalized["tmdb_id"]:
                results.append(normalized)
        if year is not None:
            results.sort(key=lambda item: (item["year"] != year, -(item["tmdb_id"] or 0)))
        return results[:20]

    async def details(self, media_type: str, tmdb_id: int) -> dict[str, Any]:
        if media_type not in {"movie", "tv"} or tmdb_id <= 0:
            raise TmdbError("TMDB ID 或媒体类型无效")
        data = await self._get(f"/{media_type}/{tmdb_id}", append_to_response="credits")
        normalized = normalize_details(data, media_type)
        if normalized is None or not normalized["tmdb_id"]:
            raise TmdbError("TMDB 未找到该作品")
        return normalized

    async def test_connection(self) -> None:
        await self._get("/configuration")

    async def download_poster(self, poster_url: str) -> tuple[bytes, str]:
        if not poster_url.startswith(TMDB_IMAGE):
            raise TmdbError("海报地址不是受信任的 TMDB 图片地址")
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, proxy=self._proxy or None
            ) as client:
                response = await client.get(poster_url)
        except httpx.TimeoutException as exc:
            logger.warning("TMDB 海报请求超时：%s", _safe_error(exc, proxy=self._proxy, key=self._key))
            raise TmdbError("下载海报失败：连接 TMDB 失败，请检查网络或代理设置") from exc
        except httpx.HTTPError as exc:
            logger.warning("TMDB 海报连接失败：%s", _safe_error(exc, proxy=self._proxy, key=self._key))
            raise TmdbError("下载海报失败：连接 TMDB 失败，请检查网络或代理设置") from exc
        if response.status_code >= 400 or not response.content:
            raise TmdbError(f"下载海报失败：HTTP {response.status_code}")
        content_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
        if content_type not in {"image/jpeg", "image/png", "image/webp"}:
            content_type = "image/jpeg"
        return response.content, content_type
