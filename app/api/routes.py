"""HTTP 接口层 —— 前端只需要这几个端点。"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app import db
from app.core import config as app_config
from app.core import datasets as ds
from app.core.contracts import STAGE_ORDER, contract_catalog
from app.core.logging import get_logger
from app.core.cron import validate as validate_cron
from app.core.llm import provider_info
from app.core.platforms import platform_list
from app.demo import build_demo_payload
from app.orchestrator import REGISTRY, run_single, run_stages
from app.scheduler import get_scheduler

router = APIRouter(prefix="/api")
log = get_logger("api")


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


# ---------------------------------------------------------------------------
# 数据接入：CSV 上传
# ---------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@router.post("/datasets/upload")
async def upload_dataset(
    kind: str = Form(..., description="products | ads | reviews"),
    file: UploadFile = File(...),
) -> dict:
    """上传并解析 CSV，返回列名映射结果。

    解析失败会明确指出「缺哪一列、当前有哪些列」，而不是一句「格式错误」。
    """
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 10MB")

    try:
        parsed = ds.parse_csv(raw, kind)
    except ds.DatasetError as exc:
        log.warning("dataset upload rejected kind=%s file=%s err=%s", kind, file.filename, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    did = db.new_run_id()
    saved = db.save_dataset(
        did, kind, file.filename or f"{kind}.csv", parsed["rows"], parsed["mapping"]
    )
    log.info(
        "dataset uploaded id=%s kind=%s rows=%s skipped=%s",
        did, kind, parsed["row_count"], parsed["skipped"],
    )
    return {
        **saved,
        "skipped": parsed["skipped"],
        "ignored_columns": parsed["ignored_columns"],
        "warnings": parsed["warnings"],
        "preview": parsed["preview"],
    }


@router.get("/datasets")
def get_datasets(kind: str | None = None) -> dict:
    return {"datasets": db.list_datasets(kind)}


@router.delete("/datasets/{did}")
def remove_dataset(did: str) -> dict:
    if not db.delete_dataset(did):
        raise HTTPException(status_code=404, detail="数据集不存在")
    log.info("dataset deleted id=%s", did)
    return {"id": did, "deleted": True}


class RunFromDatasets(BaseModel):
    product_dataset_id: str | None = None
    ad_dataset_id: str | None = None
    review_dataset_id: str | None = None
    meta: dict = Field(default_factory=dict)


@router.post("/runs/from-datasets")
def run_from_datasets(req: RunFromDatasets) -> dict:
    """用已上传的数据集组装并执行。

    缺哪份数据就不塞哪份 —— 下游 Stage 会自己报 INPUT_MISSING，
    这里绝不补假数据。
    """
    def rows_of(did: str | None, label: str) -> list[dict] | None:
        if not did:
            return None
        rows = db.load_dataset_rows(did)
        if rows is None:
            raise HTTPException(status_code=404, detail=f"{label}数据集不存在或已删除")
        return rows

    products = rows_of(req.product_dataset_id, "商品")
    ads = rows_of(req.ad_dataset_id, "广告")
    reviews = rows_of(req.review_dataset_id, "评论")

    if not any([products, ads, reviews]):
        raise HTTPException(status_code=400, detail="至少要选择一份数据集")

    payload = ds.build_payload(products=products, ads=ads, reviews=reviews, meta=req.meta)
    log.info(
        "run from datasets products=%s ads=%s reviews=%s",
        len(products or []), len(ads or []), len(reviews or []),
    )
    return run_stages(payload)


# ---------------------------------------------------------------------------
# 阈值配置
# ---------------------------------------------------------------------------

@router.get("/config")
def get_config(platform: str | None = None) -> dict:
    """当前生效的阈值清单，每项带来源（请求/界面/平台/默认）。"""
    cfg = app_config.load_config(platform)
    groups: dict[str, list] = {}
    for item in cfg.as_list():
        groups.setdefault(item["group"], []).append(item)
    return {
        "platform": platform or "",
        "groups": [{"name": k, "items": v} for k, v in groups.items()],
        "overrides": db.get_settings(),
        "platform_overrides": app_config.PLATFORM_OVERRIDES,
    }


class ConfigUpdate(BaseModel):
    values: dict


@router.put("/config")
def update_config(req: ConfigUpdate) -> dict:
    """更新覆蓋值。传 null 表示恢复该项默认。"""
    result = app_config.update_overrides(req.values)
    log.info("config updated: %s", result)
    if result["rejected"]:
        raise HTTPException(
            status_code=400,
            detail=f"以下配置项非法或超出范围：{', '.join(result['rejected'])}",
        )
    return result


@router.post("/config/reset")
def reset_config() -> dict:
    app_config.reset_all()
    log.info("config reset to defaults")
    return {"reset": True}
