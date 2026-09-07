"""单容器入口：一个进程里同时监听管理端口与用户端端口。

以前是 compose 起两个容器，各跑一个 uvicorn。现在合成一个容器，
但**没有**把两套路由合进同一个 app —— 双端口隔离是这个项目的安全基线：
管理后台的路由只注册在 admin app 上，portal 端口上根本不存在 /users、/settings，
所以 portal 端口即使反代到公网，也不可能靠猜路径进管理页。

实现方式是在同一个 asyncio 事件循环里跑两个 uvicorn.Server，各自绑一个端口。
定时任务仍然只挂在 admin app 的 lifespan 上（见 main.py），
所以清理任务只会跑一遍，不存在重复执行。
"""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from .config import get_settings
from .logging_config import configure_logging

logger = logging.getLogger(__name__)


def _server(target: str, port: int) -> uvicorn.Server:
    config = uvicorn.Config(
        target,
        host="0.0.0.0",
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_config=None,
    )
    return uvicorn.Server(config)


async def _run() -> None:
    settings = get_settings()
    admin = _server("app.main:app", settings.admin_port)
    portal = _server("app.portal_main:app", settings.portal_port)

    logger.info(
        "启动中：管理后台 :%s，用户端 :%s（同一进程，两个监听）",
        settings.admin_port,
        settings.portal_port,
    )

    tasks = [
        asyncio.create_task(admin.serve(), name="admin"),
        asyncio.create_task(portal.serve(), name="portal"),
    ]

    # 任一监听意外退出（端口被占、启动失败）就整体收摊，
    # 免得容器还"活着"但其实只有一半能用，健康检查也看不出来。
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    for task in done:
        if exc := task.exception():
            logger.error("%s 监听异常退出：%s", task.get_name(), exc)

    admin.should_exit = True
    portal.should_exit = True
    for task in pending:
        try:
            await asyncio.wait_for(task, timeout=10)
        except (TimeoutError, asyncio.TimeoutError):
            task.cancel()
        except asyncio.CancelledError:
            pass

    for task in done:
        if task.exception():
            raise SystemExit(1)


def main() -> None:
    configure_logging()
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
