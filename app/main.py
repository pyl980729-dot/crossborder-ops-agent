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

# 不需要鉴权的路径
PUBLIC_PATHS = {"/", "/api/health", "/favicon.ico", "/docs", "/openapi.json", "/redoc"}


def _check_auth(request: Request) -> JSONResponse | None:
    """返回 None 表示放行。没配置 token 时永远放行。"""
    token = os.getenv("ECOM_API_TOKEN", "").strip()
    if not token:
        return None

    path = request.url.path
    if not path.startswith("/api") or path in PUBLIC_PATHS:
        return None

    provided = request.headers.get("X-API-Token", "")
    if not provided:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()

    try:
        ok = bool(provided) and secrets.compare_digest(provided, token)
    except TypeError:  # 非 ASCII token，compare_digest 会拒
        ok = provided == token

    if ok:
        return None
    return JSONResponse(
        {"detail": "未授权：请在请求头带上 X-API-Token"},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


@app.middleware("http")
async def request_pipeline(request: Request, call_next):
    rid = uuid4().hex[:8]
    request.state.request_id = rid
    started = time.perf_counter()

    denied = _check_auth(request)
    if denied is not None:
        ms = (time.perf_counter() - started) * 1000
        log.warning("[%s] %s %s → 401 (%.0fms)", rid, request.method, request.url.path, ms)
        denied.headers["X-Request-ID"] = rid
        return denied

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
    token_on = bool(os.getenv("ECOM_API_TOKEN", "").strip())
    log.info("服务启动 | 鉴权=%s | 调度器=%s", "开启" if token_on else "关闭(开放模式)", get_scheduler().state)


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
