"""Static Vue entry points for the admin and portal applications.

The two ASGI applications intentionally receive different routers.  Keeping
the shell boundary separate means a portal listener never exposes an admin
page (and vice versa), even when both listeners share one process.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

_FRONTEND = Path(__file__).parent / "static" / "frontend"


def _entry(name: str) -> FileResponse:
    path = _FRONTEND / f"{name}.html"
    return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-store"})


def create_shell_router(scope: str) -> APIRouter:
    """Return the static-page router for one listener scope."""
    router = APIRouter()

    async def portal_shell() -> FileResponse:
        return _entry("portal")

    async def admin_shell() -> FileResponse:
        return _entry("admin")

    if scope == "portal":
        for path in ("/login", "/register", "/account", "/requests"):
            router.add_api_route(
                path,
                portal_shell,
                methods=["GET"],
                include_in_schema=False,
                response_class=FileResponse,
            )
    elif scope == "admin":
        for path in (
            "/login",
            "/dashboard",
            "/users",
            "/servers",
            "/requests",
            "/history",
            "/logs",
            "/codes",
            "/settings",
        ):
            router.add_api_route(
                path,
                admin_shell,
                methods=["GET"],
                include_in_schema=False,
                response_class=FileResponse,
            )
    else:
        raise ValueError(f"unknown shell scope: {scope}")
    return router


# Backwards-compatible import for small external integrations.  Applications
# should use an explicit scope so the listener boundary remains obvious.
router = create_shell_router("admin")
admin_shell_router = router
portal_shell_router = create_shell_router("portal")


__all__ = ["router", "admin_shell_router", "portal_shell_router", "create_shell_router"]
