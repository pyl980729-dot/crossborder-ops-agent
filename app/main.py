"""应用入口：FastAPI + 静态前端。

三层保护，按需开启：
1. 鉴权（配了 ECOM_API_TOKEN 才生效，不配就是开放模式，保证开箱即用）
2. 请求日志（每个请求一个 request id，401 也记）
3. 全局异常兜底（返回 request id，方便对着日志查）
"""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import db
from app.api.deps import resolve_principal
from app.api.routes import router
from app.core.contracts import STAGE_ORDER
from app.core.logging import get_logger, setup_logging
from app.scheduler import autostart, get_scheduler

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

setup_logging(os.getenv("ECOM_LOG_LEVEL", "INFO"))
log = get_logger("app")

app = FastAPI(
    title="CrossBorder Ops Agent",
    description="跨境电商运营多 Agent 系统：7 个 Stage 覆盖从选品到复盘的全链路，"
                "所有数字由确定性脚本计算，Agent 只做解读与决策建议。",
    version="0.2.0",
)

# 允许跨域来源可通过环境变量收紧；默认 * 只适合本机开发
_origins = os.getenv("ECOM_CORS_ORIGINS", "").strip()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins.split(",")] if _origins else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 不需要登录的路径
PUBLIC_PATHS = {
    "/", "/api/health", "/favicon.ico", "/docs", "/openapi.json", "/redoc",
    "/api/auth/login", "/api/auth/setup", "/api/auth/me", "/api/auth/status",
}


def _login_required(request: Request, principal) -> bool:
    """系统里一旦有了用户，/api 就必须登录。

    「开放模式」（无用户 + 无服务令牌）视为 owner，保证 clone 下来直接能用。
    """
    if principal.authenticated or principal.via == "open":
        return False
    path = request.url.path
    return path.startswith("/api") and path not in PUBLIC_PATHS


@app.middleware("http")
async def request_pipeline(request: Request, call_next):
    rid = uuid4().hex[:8]
    request.state.request_id = rid
    started = time.perf_counter()

    principal = resolve_principal(request)
    request.state.principal = principal

    if _login_required(request, principal):
        ms = (time.perf_counter() - started) * 1000
        log.warning("[%s] %s %s → 401 未登录 (%.0fms)", rid, request.method, request.url.path, ms)
        resp = JSONResponse(
            {"detail": "未登录：请先登录", "need_login": True}, status_code=401
        )
        resp.headers["X-Request-ID"] = rid
        return resp

    try:
        response = await call_next(request)
    except Exception:  # noqa: BLE001 兜底：不能让异常变成没有 request id 的 500
        log.exception("[%s] %s %s 未捕获异常", rid, request.method, request.url.path)
        return JSONResponse(
            {"detail": "服务器内部错误", "request_id": rid}, status_code=500
        )

    ms = (time.perf_counter() - started) * 1000
    level = log.warning if response.status_code >= 400 else log.info
    level("[%s] %s %s → %s (%.0fms)", rid, request.method, request.url.path, response.status_code, ms)
    response.headers["X-Request-ID"] = rid
    return response


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    autostart()
    users = db.count_users()
    mode = "已初始化用户，需登录" if users else "开放模式（无用户，可直接访问）"
    log.info("服务启动 | 访问模式=%s | 用户数=%s | 调度器=%s", mode, users, get_scheduler().state)


@app.on_event("shutdown")
def _shutdown() -> None:
    get_scheduler().stop()
    log.info("服务停止")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/pipeline")
def pipeline() -> dict:
    """流水线定义（给前端画图用）。"""
    return {"order": STAGE_ORDER, "animated": True}
