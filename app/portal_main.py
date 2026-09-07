"""用户端 portal 进程入口。

独立进程 + 独立端口 + 独立 cookie 名，只挂载 portal 静态壳和版本化 API。
管理后台的路由在这里一条都没有注册，所以即使本端口暴露在公网，
也不存在"猜到 /users 就能进管理页"的可能。

后台定时任务（未激活清理、到期回收）只由管理进程的 scheduler 跑，
这里不启动，避免两个进程重复执行同一批清理。
"""

import logging
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .db import init_db
from .shell import portal_shell_router
from .security import PORTAL_COOKIE_NAME
from .api import create_api_router
from .logging_config import configure_logging

configure_logging()

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # create_all 带 checkfirst，与管理进程并存也不会互相覆盖。
    await init_db()
    yield


app = FastAPI(
    title="Emby Apex 用户中心",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie=PORTAL_COOKIE_NAME,
    max_age=settings.session_max_age_seconds,
    same_site="lax",
    https_only=settings.cookie_secure,
)

app.include_router(portal_shell_router)
app.include_router(create_api_router("portal"))


@app.exception_handler(HTTPException)
async def portal_exception_handler(_request: Request, exc: HTTPException):
    """未登录时依赖里抛的 303 转成真正的跳转，其余错误给一张纯文本小页面。"""
    location = (exc.headers or {}).get("Location")
    if location:
        return RedirectResponse(location, status_code=303)
    return HTMLResponse(
        f"<!DOCTYPE html><meta charset=utf-8>"
        f"<p style='font:15px system-ui;padding:24px'>{exc.detail}"
        f"<br><a href='/account'>返回账户中心</a></p>",
        status_code=exc.status_code,
    )


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": "Emby Apex 用户中心",
            "short_name": "Apex 用户中心",
            "start_url": "/account",
            "scope": "/",
            "display": "standalone",
            "background_color": "#0b0912",
            "theme_color": "#0b0912",
            "icons": [
                {"src": "/static/logoicon.png", "sizes": "100x100", "type": "image/png"},
                {"src": "/static/brand-logo.png", "sizes": "1280x1280", "type": "image/png"},
            ],
        }
    )


@app.get("/sw.js", include_in_schema=False)
async def service_worker() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "sw.js", media_type="application/javascript")


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/account", status_code=302)
