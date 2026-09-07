import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import scheduler
from .config import get_settings
from .db import init_db
from .shell import admin_shell_router
from .security import ADMIN_COOKIE_NAME
from .api import create_api_router
from .api.wechat import router as wechat_router
from .webhook import router as webhook_router
from .logging_config import configure_logging

configure_logging()

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    scheduler.start()
    try:
        yield
    finally:
        await scheduler.shutdown()


app = FastAPI(title="Emby Apex", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie=ADMIN_COOKIE_NAME,
    max_age=settings.session_max_age_seconds,
    same_site="lax",
    https_only=settings.cookie_secure,
)

app.include_router(admin_shell_router)
app.include_router(create_api_router("admin"))
app.include_router(wechat_router)
app.include_router(webhook_router)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": "Emby Apex",
            "short_name": "Emby Apex",
            "start_url": "/dashboard",
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
    return RedirectResponse("/dashboard", status_code=302)
