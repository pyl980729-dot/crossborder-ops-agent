"""应用入口：FastAPI + 静态前端。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import db
from app.api.routes import router
from app.core.contracts import STAGE_ORDER
from app.scheduler import get_scheduler, autostart

BASE_DIR = __import__("pathlib").Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="CrossBorder Ops Agent",
    description="跨境电商运营多 Agent 系统：7 个 Stage 覆盖从选品到复盘的全链路，"
                "所有数字由确定性脚本计算，Agent 只做解读与决策建议。",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    autostart()


@app.on_event("shutdown")
def _shutdown() -> None:
    get_scheduler().stop()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/pipeline")
def pipeline() -> dict:
    """流水线定义（给前端画图用）。"""
    return {
        "order": STAGE_ORDER,
        "animated": True,
    }
