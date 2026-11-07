"""闭环对比 —— 让「优化」这件事有可验证的下一步。

这里不做的事：不自动改价、不自动停广告。那些需要人审批。
这里做的事：**自动回答「上次说要改的，改了没有，改了有没有用」**。

三个产物：
1. 指标对比：本次 vs 上次，每个关键指标涨跌多少、算改善还是恶化
2. 遗留动作：上次的 P0 这次还在不在 —— 在，说明没执行或没效果
3. 整体判定：改善 / 恶化 / 持平
"""

from __future__ import annotations

from typing import Any

# (键, 中文名, 方向, 小数位)
# 方向 up = 越大越好；down = 越小越好
KEY_METRICS: list[tuple[str, str, str, int]] = [
    ("ads_roas", "广告整体 ROAS", "up", 2),
    ("ads_p0", "P0 失血组", "down", 0),
    ("review_good_rate", "好评率", "up", 4),
    # 用「差评率」而不是「可修复率」：可修复率下降可能是好事（误解型修完了），
    # 拿它当改善指标会给出反向结论。差评率越低越好，没有歧义。
    ("review_bad_rate", "差评率", "down", 4),
    ("action_total", "动作总数", "down", 0),
    ("p0_total", "P0 动作数", "down", 0),
]


def snapshot(result: dict) -> dict[str, Any]:
    """从一次运行结果里抽出可对比的指标快照。"""
    stages = {s["stage"]: s for s in result.get("stages", [])}
    snap: dict[str, Any] = {}

    ads = stages.get("ads", {}).get("metrics", {})
    if ads:
        snap["ads_roas"] = ads.get("summary", {}).get("overall_roas", 0)
        snap["ads_p0"] = ads.get("summary", {}).get("p0_count", 0)

    review = stages.get("review", {}).get("metrics", {})
    if review:
        agg = review.get("aggregate", {})
        snap["review_good_rate"] = agg.get("good_rate", 0)
        snap["review_bad_rate"] = agg.get("bad_rate", 0)

    summary = result.get("summary", {})
    snap["action_total"] = summary.get("action_total", 0)
    snap["p0_total"] = summary.get("p0_total", 0)

    return snap


def _action_key(a: dict) -> str:
    """动作指纹：优先级 + 对象。不比对文案，因为措辞会随数据微调。"""
    return f"{a.get('priority', '')}|{(a.get('target') or '').strip()}"


def collect_actions(result: dict, priority: str = "P0") -> dict[str, dict]:
    out: dict[str, dict] = {}
    for stage in result.get("stages", []):
        for a in stage.get("actions", []):
            if a.get("priority") == priority:
                out.setdefault(_action_key(a), a)
    return out


def _round(value: float, digits: int):
    """整数指标不要显示成 2.0 —— 展示层的小事，但影响可读性。"""
    rounded = round(value, digits)
    return int(rounded) if digits == 0 else rounded


def compare(prev: dict, curr: dict) -> dict:
    """对比两次运行，产出可直接展示的结论。"""
    prev_snap = prev.get("snapshot", {})
    curr_snap = curr.get("snapshot", {})

    if not prev_snap:
        return {
            "has_baseline": False,
            "verdict": "首次运行",
            "reason": "没有历史基线，本次结果将作为下次对比的基准",
            "rows": [],
            "score": {"better": 0, "worse": 0, "same": 0},
            "pending": {"stale": [], "resolved": [], "new": []},
        }

    rows = []
    better = worse = same = 0
    for key, label, direction, digits in KEY_METRICS:
        if key not in prev_snap or key not in curr_snap:
            continue
        before = float(prev_snap[key] or 0)
        after = float(curr_snap[key] or 0)
        delta = after - before
        delta_pct = (delta / before) if before else 0.0

        if abs(delta) < 1e-9:
            trend = "same"
            same += 1
        elif (delta > 0) == (direction == "up"):
            trend = "better"
            better += 1
        else:
            trend = "worse"
            worse += 1

        rows.append(
            {
                "key": key,
                "label": label,
                "prev": _round(before, digits),
                "curr": _round(after, digits),
                "delta": _round(delta, digits),
                "delta_pct": round(delta_pct, 4),
                "trend": trend,
                "direction": direction,
            }
        )

    # 遗留动作追踪
    prev_p0 = collect_actions(prev.get("result", {}))
    curr_p0 = collect_actions(curr.get("result", {}))

    stale = [prev_p0[k] for k in prev_p0 if k in curr_p0]
    resolved = [prev_p0[k] for k in prev_p0 if k not in curr_p0]
    new = [curr_p0[k] for k in curr_p0 if k not in prev_p0]

    if better > worse:
        verdict, reason = "改善", f"{better} 项指标转好，{worse} 项转差"
    elif worse > better:
        verdict, reason = "恶化", f"{worse} 项指标转差，{better} 项转好"
    else:
        verdict, reason = "持平", f"{better} 项转好、{worse} 项转差，净变化为零"

    if stale:
        reason += f"；{len(stale)} 条 P0 动作仍未消除"

    return {
        "has_baseline": True,
        "verdict": verdict,
        "reason": reason,
        "rows": rows,
        "score": {"better": better, "worse": worse, "same": same},
        "pending": {"stale": stale, "resolved": resolved, "new": new},
    }


def baseline_payload(prev_run: dict) -> dict:
    """把数据库里的历史运行整理成 compare 需要的形状。"""
    return {
        "snapshot": snapshot(prev_run.get("result", {})),
        "result": prev_run.get("result", {}),
        "run_id": prev_run.get("id"),
        "created_at": prev_run.get("created_at"),
    }


def current_payload(result: dict) -> dict:
    return {"snapshot": snapshot(result), "result": result, "run_id": result.get("run_id")}
