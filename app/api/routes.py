"""HTTP 接口层 —— 前端只需要这几个端点。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import db
from app.core.contracts import STAGE_ORDER, contract_catalog
from app.core.cron import validate as validate_cron
from app.core.llm import provider_info
from app.core.platforms import platform_list
from app.demo import build_demo_payload
from app.orchestrator import REGISTRY, run_single, run_stages
from app.scheduler import get_scheduler

router = APIRouter(prefix="/api")


class RunRequest(BaseModel):
    payload: dict | None = Field(default=None, description="不传则使用内置演示数据")
    stages: list[str] | None = Field(default=None, description="只跑部分 Stage")


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "llm": provider_info(), "stages": len(STAGE_ORDER)}


@router.get("/contracts")
def contracts() -> dict:
    return {"contracts": contract_catalog(), "order": STAGE_ORDER}


@router.get("/platforms")
def platforms() -> dict:
    return {"platforms": platform_list()}


@router.get("/demo")
def demo() -> dict:
    payload = build_demo_payload()
    # 商品列表太长，前端默认不展示全量，但保留完整数据供运行
    preview = {k: v for k, v in payload.items() if k not in ("products", "reviews", "campaigns")}
    return {
        "payload": payload,
        "preview": preview,
        "counts": {
            "products": len(payload["products"]),
            "campaigns": len(payload["campaigns"]),
            "reviews": len(payload["reviews"]),
        },
    }


@router.post("/runs")
def create_run(req: RunRequest) -> dict:
    payload = req.payload or build_demo_payload()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload 必须是对象")
    try:
        return run_stages(payload, stages=req.stages)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runs")
def list_runs(limit: int = 20) -> dict:
    return {"runs": db.list_runs(limit=limit), "stats": db.stats()}


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return run


@router.post("/stages/{stage}")
def run_stage(stage: str, payload: dict) -> dict:
    """单点重跑 —— 回流后只重做被影响的环节，不重跑全链路。"""
    if stage not in REGISTRY:
        raise HTTPException(status_code=404, detail=f"未知 Stage: {stage}")
    return run_single(stage, payload)


@router.get("/stages")
def stages() -> dict:
    return {
        "stages": [
            {
                "key": key,
                "title": REGISTRY[key].title,
                "required": REGISTRY[key].required,
            }
            for key in STAGE_ORDER
        ]
    }


# ---------------------------------------------------------------------------
# 自动化：定时任务 + 闭环对比
# ---------------------------------------------------------------------------

class ScheduleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    cron: str = Field(default="0 9 * * *", description="分 时 日 月 周，例如 0 9 * * *")
    payload: dict | None = Field(default=None, description="不传则每次用内置演示数据")
    enabled: bool = True


@router.get("/automation/status")
def automation_status() -> dict:
    sched = get_scheduler()
    return {**sched.status(), "stats": db.stats()}


@router.post("/automation/schedules")
def create_schedule(req: ScheduleRequest) -> dict:
    ok, message = validate_cron(req.cron)
    if not ok:
        raise HTTPException(status_code=400, detail=f"cron 非法：{message}")

    sid = db.new_run_id()
    payload = req.payload or {}
    db.create_schedule(sid, req.name, req.cron, payload)
    if not req.enabled:
        db.set_schedule_enabled(sid, False)

    return {"id": sid, "next_run_at": get_scheduler().status()}


@router.patch("/automation/schedules/{sid}")
def update_schedule(sid: str, enabled: bool) -> dict:
    if not db.get_schedule(sid):
        raise HTTPException(status_code=404, detail="任务不存在")
    db.set_schedule_enabled(sid, enabled)
    return {"id": sid, "enabled": enabled}


@router.delete("/automation/schedules/{sid}")
def remove_schedule(sid: str) -> dict:
    if not db.get_schedule(sid):
        raise HTTPException(status_code=404, detail="任务不存在")
    db.delete_schedule(sid)
    return {"id": sid, "deleted": True}


@router.post("/automation/schedules/{sid}/run")
def run_schedule_now(sid: str) -> dict:
    """立即执行一次（不等 cron 到点），用于验证配置是否正确。"""
    full = db.get_schedule(sid)
    if not full:
        raise HTTPException(status_code=404, detail="任务不存在")

    from datetime import datetime

    payload = full["payload"] or build_demo_payload()
    result = run_stages(payload, schedule_id=sid, trigger="manual")
    db.touch_schedule(sid, result["run_id"], datetime.now().isoformat(timespec="seconds"))
    return result


@router.post("/automation/tick")
def automation_tick() -> dict:
    """手动触发一次调度检查，不用等到下一个 tick 周期。"""
    executed = get_scheduler().tick()
    return {"executed": executed, "status": get_scheduler().status()}
