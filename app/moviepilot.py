"""Small MoviePilot V3 API client used by the portal request flow."""
from __future__ import annotations

import asyncio, time
from typing import Any
from urllib.parse import quote
import httpx

from . import settings_store
from .config import get_settings
from .logging_config import register_sensitive_values

class MoviePilotError(RuntimeError):
    pass


def _source(value: str | None) -> str:
    """Return the canonical MoviePilot media provider name.

    Older Apex payloads used ``tmdb`` while MoviePilot V3 identifies the
    provider as ``themoviedb``.  Normalising at the client boundary keeps all
    endpoint calls consistent and avoids otherwise opaque 422 responses.
    """

    normalized = str(value or "").strip().lower()
    return "themoviedb" if normalized in {"tmdb", "themoviedb"} else normalized

_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}

def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and {"success", "data"}.issubset(payload):
        if not payload.get("success"):
            raise MoviePilotError(str(payload.get("message") or "MoviePilot 请求失败"))
        return payload.get("data")
    return payload


def _safe_message(value: object, *secrets: str | None) -> str:
    """Compact a remote error while removing credentials echoed by a proxy."""

    message = " ".join(str(value or "").split())[:300]
    for secret in secrets:
        candidate = str(secret or "")
        if candidate:
            message = message.replace(candidate, "[REDACTED]")
    return message

class MoviePilotClient:
    def __init__(self, *, timeout: float | None = None, url: str | None = None, username: str | None = None, password: str | None = None):
        runtime = settings_store.current()
        base = str(url if url is not None else runtime.moviepilot_url or "").strip().rstrip("/")
        if not base:
            raise MoviePilotError("管理员尚未配置 MoviePilot 地址")
        if not base.startswith(("http://", "https://")):
            raise MoviePilotError("MoviePilot 地址无效")
        self.base = base + ("" if base.endswith("/api/v1") else "/api/v1")
        self.username = str(username if username is not None else runtime.moviepilot_username or "").strip()
        self.password = str(password if password is not None else runtime.moviepilot_password or "")
        if not self.username or not self.password:
            raise MoviePilotError("管理员尚未配置 MoviePilot 用户名或密码")
        self.timeout = timeout or get_settings().http_timeout_seconds
        self._token: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._login_lock = asyncio.Lock()
        register_sensitive_values(self.password)
        cached = _TOKEN_CACHE.get((self.base, self.username))
        if cached and cached[1] > time.monotonic(): self._token = cached[0]

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=self.timeout, headers={"Accept": "application/json"})
        return self
    async def __aexit__(self, *_):
        if self._client: await self._client.aclose()

    async def _login(self) -> None:
        assert self._client
        try:
            response = await self._client.post(f"{self.base}/login/access-token", data={"username": self.username, "password": self.password})
            response.raise_for_status(); payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise MoviePilotError("MoviePilot 登录失败，请检查用户名和密码") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise MoviePilotError("MoviePilot 登录失败") from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token: raise MoviePilotError("MoviePilot 登录响应无效")
        self._token = str(token); _TOKEN_CACHE[(self.base, self.username)] = (self._token, time.monotonic() + 300); register_sensitive_values(self._token)

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        assert self._client
        for attempt in range(2):
            if not self._token:
                async with self._login_lock:
                    if not self._token: await self._login()
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["Authorization"] = f"Bearer {self._token}"
            try:
                response = await self._client.request(method, f"{self.base}/{path.lstrip('/')}", headers=headers, **kwargs)
                if response.status_code == 401:
                    self._token = None
                    _TOKEN_CACHE.pop((self.base, self.username), None)
                    if attempt == 0: continue
                response.raise_for_status()
                return _unwrap(response.json())
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    raise MoviePilotError("MoviePilot 登录已失效") from exc
                # FastAPI validation errors and V3 business failures include
                # useful diagnostics in the response body.  Preserve only a
                # short, textual message; never include request headers,
                # credentials, or the raw response in the exception.
                message = ""
                try:
                    body = exc.response.json()
                    if isinstance(body, dict):
                        message = str(body.get("message") or body.get("detail") or "")
                        if isinstance(body.get("detail"), list):
                            message = "; ".join(
                                str(item.get("msg") or item)
                                for item in body["detail"][:3]
                                if isinstance(item, dict)
                            )
                    elif isinstance(body, str):
                        message = body
                except (ValueError, TypeError):
                    message = ""
                message = _safe_message(message, self.password, self._token)
                suffix = f": {message}" if message else ""
                raise MoviePilotError(
                    f"MoviePilot 请求失败（HTTP {exc.response.status_code}）{suffix}"
                ) from exc
            except (httpx.HTTPError, ValueError) as exc:
                raise MoviePilotError("MoviePilot 网络请求失败") from exc
        raise MoviePilotError("MoviePilot 登录已失效")

    async def search_media(self, title: str, *, media_type: str | None = None, page: int = 1, count: int = 8) -> list[dict[str, Any]]:
        params = {"title": title, "type": "media", "page": page, "count": count}
        # media_source is a data provider (for example ``tmdb``), not the
        # movie/TV kind; MoviePilot returns both kinds when omitted.
        data = await self._request("GET", "media/search", params=params)
        if isinstance(data, list): return data
        if isinstance(data, dict):
            for key in ("results", "items", "medias", "data"):
                if isinstance(data.get(key), list): return data[key]
        return []

    async def detail(self, media_source: str, media_id: str, media_type: str | None = None) -> dict[str, Any]:
        source = _source(media_source)
        data = await self._request(
            "GET",
            f"media/{quote(str(media_id), safe='')}",
            params={"media_source": source, "type_name": media_type or ""},
        )
        return data if isinstance(data, dict) else {}

    async def exists(self, *, mtype: str, media_source: str, media_id: str, season: int | None = None, title: str = "", year: int | None = None) -> bool | None:
        params = {"mtype": mtype, "media_source": _source(media_source), "media_id": media_id, "title": title, "year": year or ""}
        if season is not None: params["season"] = season
        data = await self._request("GET", "mediaserver/exists", params=params)
        if not isinstance(data, dict) or "item" not in data: return None
        return data.get("item") is not None

    async def subscribe(self, *, name: str, media_type: str, media_source: str, media_id: str, year: int | None = None, season: int | None = None) -> int:
        # ``season`` is optional in V3 but sending an explicit null for movies
        # matches the native frontend request model and avoids strict schema
        # variants treating the field as missing.
        payload = {
            "name": name,
            "type": media_type,
            "year": str(year or ""),
            "media_source": _source(media_source),
            "media_id": str(media_id),
            "season": season,
        }
        try:
            data = await self._request("POST", "subscribe/", json=payload)
        except MoviePilotError as exc:
            # V3 accepts the subscription object as form data on some builds.
            if "422" not in str(exc) and "400" not in str(exc): raise
            data = await self._request(
                "POST",
                "subscribe/",
                data={k: "" if v is None else str(v) for k, v in payload.items()},
            )
        value = data.get("id") if isinstance(data, dict) else data
        try: return int(value)
        except (TypeError, ValueError) as exc: raise MoviePilotError("MoviePilot 订阅响应无效") from exc

    async def pause(self, subscribe_id: int) -> None:
        await self._request("PUT", f"subscribe/status/{subscribe_id}", params={"state": "S"})
