"""编排器 —— 把 7 个 Stage 串成一条有方向的流水线，并且真的会回头。

三个职责：
1. 顺序：按 STAGE_ORDER 执行，前一步的输出自动补进后一步的输入
2. 回流：收集 ads/review 的 feedback，标出哪些上游环节需要重做
3. 留痕：全过程落库，任何一次运行都能复现
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from app import db
from app.agents.ads import AdsAgent
from app.agents.base import StageAgent
from app.agents.image import ImageAgent
from app.agents.listing import ListingAgent
from app.agents.market import MarketAgent
from app.agents.review import ReviewAgent
from app.agents.strategy import StrategyAgent
from app.agents.video import VideoAgent
from app.core import cycle
from app.core.contracts import BACKFLOW_MAP, STAGE_ORDER, StageResult

REGISTRY: dict[str, StageAgent] = {
    "market": MarketAgent(),
    "strategy": StrategyAgent(),
    "listing": ListingAgent(),
    "image": ImageAgent(),
    "video": VideoAgent(),
    "ads": AdsAgent(),
    "review": ReviewAgent(),
}


def get_agent(stage: str) -> StageAgent:
    if stage not in REGISTRY:
        raise KeyError(f"未知 Stage: {stage}")
    return REGISTRY[stage]


def _derive(payload: dict, results: dict[str, StageResult]) -> dict:
    """补齐下游需要的字段 —— 让用户不必把同一个价格填两遍。

    只做「有明确来源」的补全，绝不凭空生成数值：
      target_price 缺失 → 取 02 算出的售价
      selling_points 缺失 → 取 03 的卖点（03 本身就要求它）
    """
    out = dict(payload)

    # 短视频的 main_point 必须与 Listing 的主卖点是同一句 —— 这是闭环一致性的关键。
    if not out.get("main_point"):
        points = out.get("selling_points") or []
        if points:
            out["main_point"] = points[0]

    strategy = results.get("strategy")
    if strategy and strategy.status == "ok":
        pricing = strategy.metrics.get("pricing", {})
        out.setdefault("target_price", pricing.get("price"))
        out.setdefault("commission_rate", pricing.get("params", {}).get("commission_rate"))
        out.setdefault("tax_rate", pricing.get("params", {}).get("tax_rate"))

    listing = results.get("listing")
    if listing and listing.status == "ok":
        best = listing.metrics.get("best", {})
        if best.get("text"):
            out.setdefault("chosen_title", best["text"])

    return out


def run_stages(
    payload: dict,
    stages: list[str] | None = None,
    run_id: str | None = None,
    schedule_id: str | None = None,
    trigger: str = "manual",
    store_id: str = "default",
) -> dict:
    """执行一次完整流水线。

    执行完会自动与上一次运行做闭环对比（同任务优先），
    所以每次运行都能回答「这次比上次好了没有」。
    """
    started = time.perf_counter()
    stages = stages or STAGE_ORDER
    run_id = run_id or db.new_run_id()
    ran_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    results: dict[str, StageResult] = {}
    ordered: list[dict] = []

    for stage in stages:
        agent = get_agent(stage)
        data = _derive(payload, results)
        result = agent.run(data)
        results[stage] = result
        ordered.append(result.model_dump())

    # ---- 收集回流 ----
    backflow: list[dict] = []
    for stage, targets in BACKFLOW_MAP.items():
        res = results.get(stage)
        if not res:
            continue
        for fb in res.feedback:
            if fb.to_stage in targets:
                backflow.append(
                    {
                        "from_stage": stage,
                        "to_stage": fb.to_stage,
                        "reason": fb.reason,
                        "scope": fb.scope,
                    }
                )

    # 去重：同一对 (from,to) 只保留第一条，附计数
    merged: dict[tuple[str, str], dict] = {}
    for item in backflow:
        key = (item["from_stage"], item["to_stage"])
        if key in merged:
            merged[key]["count"] += 1
            merged[key]["reasons"].append(item["reason"])
        else:
            merged[key] = {**item, "count": 1, "reasons": [item["reason"]]}
    backflow = list(merged.values())

    blocked = [r.stage for r in results.values() if r.status == "blocked"]
    partial = [r.stage for r in results.values() if r.status == "partial"]
    status = "ok" if not blocked and not partial else ("blocked" if all(r.status == "blocked" for r in results.values()) else "partial")

    elapsed = int((time.perf_counter() - started) * 1000)

    summary = {
        "stage_count": len(ordered),
        "ok": sum(1 for r in ordered if r["status"] == "ok"),
        "partial": len(partial),
        "blocked": len(blocked),
        "action_total": sum(len(r["actions"]) for r in ordered),
        "p0_total": sum(1 for r in ordered for a in r["actions"] if a["priority"] == "P0"),
        "insight_total": sum(len(r["insights"]) for r in ordered),
        "blocked_stages": blocked,
        "backflow_count": len(backflow),
    }

    title = payload.get("category") or payload.get("product_name") or "未命名运行"

    result_payload = {
        "run_id": run_id,
        "title": str(title),
        "status": status,
        "elapsed_ms": elapsed,
        "created_at": ran_at,
        "stages": ordered,
        "backflow": backflow,
        "summary": summary,
        "stages_order": stages,
        "trigger": trigger,
        "schedule_id": schedule_id,
        "store_id": store_id,
    }

    # ---- 闭环对比：这次比上次好了没有 ----
    prev = db.previous_run(schedule_id, run_id, store_id=store_id)
    if prev:
        cycle_report = cycle.compare(cycle.baseline_payload(prev), cycle.current_payload(result_payload))
        cycle_report["baseline_run_id"] = prev["id"]
        cycle_report["baseline_at"] = prev["created_at"]
    else:
        cycle_report = cycle.compare({}, cycle.current_payload(result_payload))
    result_payload["cycle"] = cycle_report

    db.save_run(
        run_id=run_id,
        title=str(title),
        status=status,
        stages=stages,
        payload=_jsonable(payload),
        result=result_payload,
        elapsed_ms=elapsed,
        llm_mode=_llm_mode(),
        schedule_id=schedule_id,
        trigger=trigger,
        store_id=store_id,
    )
    return result_payload


def run_single(stage: str, payload: dict) -> dict:
    """只跑一个 Stage —— 用于回流后单点重跑。

    注意：这里必须和全链路走同一套字段继承，否则会出现
    「全链路能跑的环节，单点重跑却报缺字段」这种自相矛盾。
    """
    data = dict(payload)
    if not data.get("main_point"):
        points = data.get("selling_points") or []
        if points:
            data["main_point"] = points[0]

    agent = get_agent(stage)
    result = agent.run(data)
    return result.model_dump()


def _llm_mode() -> str:
    from app.core.llm import provider_info

    return provider_info()["mode"]


def _jsonable(payload: dict) -> dict:
    """确保能落库：去掉不可序列化对象。"""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        try:
            import json

            json.dumps(value, ensure_ascii=False)
            out[key] = value
        except (TypeError, ValueError):
            out[key] = str(value)
    return out
