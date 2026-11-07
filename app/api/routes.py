"""HTTP 接口层 —— 前端只需要这几个端点。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from app import db
from app.api.deps import get_principal, require
from app.core import auth
from app.core.auth import Principal
from app.core import config as app_config
from app.core import datasets as ds
from app.core.contracts import STAGE_ORDER, contract_catalog
from app.core.logging import get_logger
from app.core.platforms import get_platform
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
def create_run(req: RunRequest, p: Principal = Depends(require("run"))) -> dict:
    payload = req.payload or build_demo_payload()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload 必须是对象")
    if not payload.get("platform"):
        store = db.get_store(p.store_id)
        if store:
            payload["platform"] = store["platform"]
    try:
        return run_stages(payload, stages=req.stages, store_id=p.store_id)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runs")
def list_runs(limit: int = 20, p: Principal = Depends(get_principal)) -> dict:
    return {
        "runs": db.list_runs(limit=limit, store_id=p.store_id),
        "stats": db.stats(p.store_id),
    }


@router.get("/runs/{run_id}")
def get_run(run_id: str, p: Principal = Depends(get_principal)) -> dict:
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    # 跨店铺不能读：靠猜 id 也拿不到别家的数据
    if run.get("result", {}).get("store_id") not in (None, p.store_id) and p.role != "owner":
        raise HTTPException(status_code=403, detail="无权访问该运行记录")
    return run


@router.post("/stages/{stage}")
def run_stage(stage: str, payload: dict, p: Principal = Depends(require("run"))) -> dict:
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
def automation_status(p: Principal = Depends(get_principal)) -> dict:
    sched = get_scheduler()
    return {**sched.status(store_id=p.store_id), "stats": db.stats(p.store_id)}


@router.post("/automation/schedules")
def create_schedule(req: ScheduleRequest, p: Principal = Depends(require("manage_schedule"))) -> dict:
    ok, message = validate_cron(req.cron)
    if not ok:
        raise HTTPException(status_code=400, detail=f"cron 非法：{message}")

    sid = db.new_run_id()
    payload = req.payload or {}
    db.create_schedule(sid, req.name, req.cron, payload, store_id=p.store_id)
    if not req.enabled:
        db.set_schedule_enabled(sid, False)

    return {"id": sid, "next_run_at": get_scheduler().status()}


@router.patch("/automation/schedules/{sid}")
def update_schedule(sid: str, enabled: bool,
                    p: Principal = Depends(require("manage_schedule"))) -> dict:
    if not db.get_schedule(sid):
        raise HTTPException(status_code=404, detail="任务不存在")
    db.set_schedule_enabled(sid, enabled)
    return {"id": sid, "enabled": enabled}


@router.delete("/automation/schedules/{sid}")
def remove_schedule(sid: str, p: Principal = Depends(require("manage_schedule"))) -> dict:
    if not db.get_schedule(sid):
        raise HTTPException(status_code=404, detail="任务不存在")
    db.delete_schedule(sid)
    return {"id": sid, "deleted": True}


@router.post("/automation/schedules/{sid}/run")
def run_schedule_now(sid: str, p: Principal = Depends(require("manage_schedule"))) -> dict:
    """立即执行一次（不等 cron 到点），用于验证配置是否正确。"""
    full = db.get_schedule(sid)
    if not full:
        raise HTTPException(status_code=404, detail="任务不存在")

    from datetime import datetime

    payload = full["payload"] or build_demo_payload()
    result = run_stages(payload, schedule_id=sid, trigger="manual", store_id=p.store_id)
    db.touch_schedule(sid, result["run_id"], datetime.now().isoformat(timespec="seconds"))
    return result


@router.post("/automation/tick")
def automation_tick(p: Principal = Depends(require("manage_schedule"))) -> dict:
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
    p: Principal = Depends(require("upload")),
) -> dict:
    """上传并解析 CSV，返回列名映射结果。

    解析失败会明确指出「缺哪一列、当前有哪些列」，而不是一句「格式错误」。
    """
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 10MB")

    # 日期顺序的兜底假设跟随店铺所在平台：巴西/欧洲是 日/月，Amazon 是 月/日
    store = db.get_store(p.store_id)
    platform = get_platform((store or {}).get("platform"))
    try:
        parsed = ds.parse_csv(raw, kind, order=platform.date_order)
    except ds.DatasetError as exc:
        log.warning("dataset upload rejected kind=%s file=%s err=%s", kind, file.filename, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    did = db.new_run_id()
    saved = db.save_dataset(
        did, kind, file.filename or f"{kind}.csv", parsed["rows"], parsed["mapping"],
        store_id=p.store_id,
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
        "date_info": parsed.get("date_info"),
        "preview": parsed["preview"],
    }


@router.get("/datasets")
def get_datasets(kind: str | None = None, p: Principal = Depends(get_principal)) -> dict:
    return {"datasets": db.list_datasets(kind, store_id=p.store_id)}


@router.delete("/datasets/{did}")
def remove_dataset(did: str, p: Principal = Depends(require("delete_data"))) -> dict:
    if not db.delete_dataset(did, store_id=p.store_id):
        raise HTTPException(status_code=404, detail="数据集不存在")
    log.info("dataset deleted id=%s", did)
    return {"id": did, "deleted": True}


class RunFromDatasets(BaseModel):
    product_dataset_id: str | None = None
    ad_dataset_id: str | None = None
    review_dataset_id: str | None = None
    meta: dict = Field(default_factory=dict)


@router.post("/runs/from-datasets")
def run_from_datasets(req: RunFromDatasets, p: Principal = Depends(require("run"))) -> dict:
    """用已上传的数据集组装并执行。

    缺哪份数据就不塞哪份 —— 下游 Stage 会自己报 INPUT_MISSING，
    这里绝不补假数据。
    """
    def rows_of(did: str | None, label: str) -> list[dict] | None:
        if not did:
            return None
        rows = db.load_dataset_rows(did, store_id=p.store_id)
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
    return run_stages(payload, store_id=p.store_id)


# ---------------------------------------------------------------------------
# 阈值配置
# ---------------------------------------------------------------------------

@router.get("/config")
def get_config(platform: str | None = None, p: Principal = Depends(get_principal)) -> dict:
    """当前生效的阈值清单，每项带来源（请求/界面/平台/默认）。

    没指定平台时用「当前店铺所属平台」—— 否则店铺开在 Amazon，
    界面却按通用默认显示 2% 的 CTR 基准，看到的和实际用的不是一回事。
    """
    if not platform:
        store = db.get_store(p.store_id)
        platform = (store or {}).get("platform")
    cfg = app_config.load_config(platform, store_id=p.store_id)
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
def update_config(req: ConfigUpdate, p: Principal = Depends(get_principal)) -> dict:
    """更新覆盖值。传 null 表示恢复该项默认。

    权限按分组区分：改 CTR 基准会影响「哪些广告算失败」，只有负责人能改；
    改标题字符上限只影响产出形态，运营也能改。
    """
    groups = set()
    for key in (req.values or {}):
        d = app_config.THRESHOLDS.get(key)
        if d:
            groups.add(d.group)

    core_groups = {g for g in groups if auth.PERMISSIONS.get(
        auth.CONFIG_GROUP_PERMISSION.get(g, "config_core"), set()
    ) == auth.PERMISSIONS["config_core"]}
    if core_groups and not p.can("config_core"):
        raise HTTPException(
            status_code=403,
            detail=f"权限不足：修改「{'、'.join(sorted(core_groups))}」的阈值需要负责人权限"
                   f"（这些参数会改变诊断结论）。当前角色：{p.role_label}。",
        )
    if groups and not (p.can("config_core") or p.can("config_content")):
        raise HTTPException(status_code=403, detail=f"权限不足：当前角色「{p.role_label}」不能修改阈值")

    result = app_config.update_overrides(req.values, store_id=p.store_id)
    log.info("config updated by %s: %s", p.username, result)
    if result["rejected"]:
        raise HTTPException(
            status_code=400,
            detail=f"以下配置项非法或超出范围：{', '.join(result['rejected'])}",
        )
    return result


@router.post("/config/reset")
def reset_config(p: Principal = Depends(require("config_core"))) -> dict:
    app_config.reset_all(p.store_id)
    log.info("config reset by %s", p.username)
    return {"reset": True}


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------

class SetupRequest(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6, max_length=64)
    display_name: str = Field(default="", max_length=32)


class LoginRequest(BaseModel):
    username: str
    password: str
    store_id: str | None = None


class NewUserRequest(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6, max_length=64)
    display_name: str = Field(default="", max_length=32)
    role: str = Field(default="operator")


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        "ecom_session", token,
        httponly=True, samesite="lax", max_age=auth.SESSION_DAYS * 24 * 3600,
    )


@router.get("/auth/status")
def auth_status() -> dict:
    """未登录也能看：系统是否已初始化、有哪些店铺。前端用它决定显示登录页还是初始化页。"""
    users = db.count_users()
    return {
        "initialized": users > 0,
        "user_count": users,
        "open_mode": users == 0 and not __import__("os").getenv("ECOM_API_TOKEN"),
        "stores": [{"id": s["id"], "name": s["name"], "platform": s["platform"]} for s in db.list_stores()],
        "roles": [
            {"key": r, "label": auth.ROLE_LABELS[r], "desc": auth.ROLE_DESC[r]}
            for r in auth.ROLES
        ],
    }


@router.post("/auth/setup")
def auth_setup(req: SetupRequest, response: Response) -> dict:
    """首次初始化：只在系统里没有任何用户时可用，创建第一个 owner。"""
    if db.count_users() > 0:
        raise HTTPException(status_code=400, detail="系统已初始化，请直接登录")

    uid = db.new_run_id()
    db.create_user(
        uid, req.username.strip(), req.display_name.strip() or req.username.strip(),
        "owner", auth.hash_password(req.password),
    )
    store_id = db.list_stores()[0]["id"]
    db.add_membership(uid, store_id)

    token = auth.new_token()
    db.create_session(token, uid, store_id, auth.session_expiry())
    _set_session_cookie(response, token)
    log.info("初始化完成: 首个 owner=%s", req.username)

    principal = db.get_user(uid)
    return {
        "token": token,
        "user": {**principal, "role_label": auth.ROLE_LABELS["owner"]},
        "store_id": store_id,
    }


@router.post("/auth/login")
def auth_login(req: LoginRequest, response: Response) -> dict:
    user = db.get_user_by_username(req.username.strip())
    if not user or not auth.verify_password(req.password, user["password_hash"]):
        log.warning("登录失败: username=%s", req.username)
        raise HTTPException(status_code=401, detail="用户名或密码不正确")

    stores = db.list_user_stores(user["id"])
    if not stores:
        raise HTTPException(status_code=403, detail="该账号未关联任何店铺")

    store_id = req.store_id or stores[0]["id"]
    if store_id not in [s["id"] for s in stores]:
        raise HTTPException(status_code=403, detail="无权访问该店铺")

    token = auth.new_token()
    db.create_session(token, user["id"], store_id, auth.session_expiry())
    _set_session_cookie(response, token)
    log.info("登录成功: %s → 店铺 %s", user["username"], store_id)

    return {
        "token": token,
        "user": {**{k: v for k, v in user.items() if k != "password_hash"},
                 "role_label": auth.ROLE_LABELS.get(user["role"], user["role"])},
        "store_id": store_id,
        "stores": stores,
    }


@router.post("/auth/logout")
def auth_logout(response: Response, token: str | None = None) -> dict:
    if token:
        db.delete_session(token)
    response.delete_cookie("ecom_session")
    return {"ok": True}


@router.get("/auth/me")
def auth_me(p: Principal = Depends(get_principal)) -> dict:
    stores = db.list_user_stores(p.user_id) if p.user_id and p.user_id not in ("local", "service") else db.list_stores()
    return {
        **p.as_dict(),
        "stores": [{"id": s["id"], "name": s["name"]} for s in stores],
        "need_login": not p.authenticated and p.via != "open",
    }


@router.post("/auth/switch-store")
def switch_store(store_id: str, request: None = None,
                 p: Principal = Depends(get_principal)) -> dict:
    """切换当前店铺：只改会话绑定的店铺，不重新登录。"""
    if not db.get_store(store_id):
        raise HTTPException(status_code=404, detail="店铺不存在")
    allowed = [s["id"] for s in db.list_user_stores(p.user_id)] if p.user_id else []
    if p.via == "open":
        allowed = [s["id"] for s in db.list_stores()]
    if store_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该店铺")

    token = request.cookies.get("ecom_session") if request else ""
    if token:
        sess = db.get_session(token)
        if sess:
            db.delete_session(token)
            db.create_session(token, sess["user_id"], store_id, sess["expires_at"])
    return {"store_id": store_id}


# ---------------------------------------------------------------------------
# 成员管理（仅 owner）
# ---------------------------------------------------------------------------

@router.get("/users")
def list_users(p: Principal = Depends(require("manage_members"))) -> dict:
    return {
        "users": db.list_store_members(p.store_id),
        "all": db.list_users() if p.is_admin else [],
        "roles": [
            {"key": r, "label": auth.ROLE_LABELS[r], "desc": auth.ROLE_DESC[r]}
            for r in auth.ROLES
        ],
    }


@router.post("/users")
def create_user(req: NewUserRequest, p: Principal = Depends(require("manage_members"))) -> dict:
    if req.role not in auth.ROLES:
        raise HTTPException(status_code=400, detail=f"角色必须是 {auth.ROLES}")
    if db.get_user_by_username(req.username.strip()):
        raise HTTPException(status_code=400, detail="用户名已存在")

    uid = db.new_run_id()
    db.create_user(
        uid, req.username.strip(), req.display_name.strip() or req.username.strip(),
        req.role, auth.hash_password(req.password),
    )
    db.add_membership(uid, p.store_id)
    log.info("新增成员: %s (%s) → 店铺 %s", req.username, req.role, p.store_id)
    return db.get_user(uid) or {}


@router.patch("/users/{uid}")
def update_user(uid: str, role: str | None = None, display_name: str | None = None,
                p: Principal = Depends(require("manage_members"))) -> dict:
    target = db.get_user(uid)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target["role"] == "owner" and role and role != "owner":
        owners = [u for u in db.list_users() if u["role"] == "owner"]
        if len(owners) <= 1:
            raise HTTPException(status_code=400, detail="至少要保留一个负责人")
    if role and role not in auth.ROLES:
        raise HTTPException(status_code=400, detail=f"角色必须是 {auth.ROLES}")

    db.update_user(uid, role=role, display_name=display_name)
    log.info("修改成员: %s → role=%s", target["username"], role)
    return db.get_user(uid) or {}


@router.delete("/users/{uid}")
def delete_user(uid: str, p: Principal = Depends(require("manage_members"))) -> dict:
    target = db.get_user(uid)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target["id"] == p.user_id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    if target["role"] == "owner":
        owners = [u for u in db.list_users() if u["role"] == "owner"]
        if len(owners) <= 1:
            raise HTTPException(status_code=400, detail="至少要保留一个负责人")

    db.delete_user(uid)
    log.info("删除成员: %s", target["username"])
    return {"id": uid, "deleted": True}


# ---------------------------------------------------------------------------
# 店铺
# ---------------------------------------------------------------------------

@router.get("/stores")
def list_stores(p: Principal = Depends(get_principal)) -> dict:
    stores = db.list_user_stores(p.user_id) if p.via == "session" else db.list_stores()
    return {"stores": stores, "current": p.store_id}


@router.post("/stores")
def create_store(name: str, platform: str = "shopee_br",
                 p: Principal = Depends(require("manage_members"))) -> dict:
    if not name.strip():
        raise HTTPException(status_code=400, detail="店铺名不能为空")
    sid = db.new_run_id()
    db.create_store(sid, name.strip(), platform)
    db.add_membership(p.user_id, sid)
    log.info("新建店铺: %s (%s)", name, sid)
    return db.get_store(sid) or {}
