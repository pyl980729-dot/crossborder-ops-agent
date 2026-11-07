"""07 评论归因 Agent —— 把「差评多」翻译成可排期的工作量。

核心指标是可修复率：
改文案/补图就能消掉的差评分出来，剩下的才是供应链问题。
这个区分直接决定接下来两周该派活给运营还是派给工厂。
"""

from __future__ import annotations

from app.agents.base import StageAgent, action, feedback, fmt_pct, insight
from app.core import config, metrics as M, timeline
from app.core.review_classify import classify_batch
from app.core.contracts import StageResult
from app.core.platforms import get_platform

# 八类归因 → 修补动作（这是运营 SOP 里最值钱的一张表）
FIX_PLAYBOOK = {
    "误解型": ("详情页 + 使用步骤图", "把误解点写进「注意事项」段，并在第 9 张图做三步图示", "listing"),
    "参数不符合预期": ("参数表 + 卖点收敛", "参数图数字必须溯源实物；卖点只讲一条，避免预期错配", "listing"),
    "配件缺失": ("包装清单图", "九宫格第 8 张改为全配件平铺俯拍并逐件标注", "image"),
    "使用困难": ("使用步骤图 + 短视频", "三步图示 + 15 秒操作视频放在详情页第二屏", "image"),
    "描述不符": ("主图/文案一致性", "主图不得美化尺寸与材质，文案删除形容性夸张", "listing"),
    "价格偏高": ("套装感知价值", "不改价，改组合：加赠配件或做两件装", "strategy"),
    "发货慢": ("物流说明前置", "不可通过内容修复，详情页前置发货时效并评估海外仓", ""),
    "破损/故障/退货": ("包装与质检", "不可通过内容修复，需升级包装与出厂检", ""),
}


class ReviewAgent(StageAgent):
    key = "review"

    def _run(self, payload: dict) -> StageResult:
        cfg = config.load_config(payload.get("platform"), payload.get("thresholds"))

        # 先自动归因，再做统计 —— 真实导出的评论表只有评分和正文，没有分类列
        reviews, cls = classify_batch(payload["reviews"], use_llm=True)
        agg = M.review_aggregate(reviews)

        insights = []
        actions = []
        feedbacks = []
        evidence = [
            "metrics.review_aggregate",
            "bad_by_category[*]",
            "timeline.analyze",
        ]

        insights.append(
            insight(
                f"共 {agg['total']} 条评论，好评率 {fmt_pct(agg['good_rate'])}，"
                f"差评 {agg['bad_count']} 条（{fmt_pct(agg['bad_rate'])}），平均分 {agg['avg_rating']}。",
                "good" if agg["good_rate"] >= cfg.get("review.good_rate_target") else "warn",
                "review_aggregate.good_rate",
            )
        )

        # ---- 自动归因情况 ----
        auto, manual, pending = cls["auto"], cls["manual"], cls["pending"]
        if auto or manual:
            parts = []
            if cls["rule_high"] or cls["rule_medium"] or cls["rule_low"]:
                parts.append(f"关键词规则 {cls['rule_high'] + cls['rule_medium'] + cls['rule_low']} 条")
            if cls["llm"]:
                parts.append(f"模型兜底 {cls['llm']} 条")
            if manual:
                parts.append(f"保留人工标注 {manual} 条")
            insights.append(
                insight(
                    f"归因覆盖率 {fmt_pct(cls['coverage'])}：{'、'.join(parts)}。"
                    f"同一批评论重跑结果完全一致（规则确定性），"
                    f"只有进了模型的那部分会因温度参数略有波动。",
                    "good" if cls["coverage"] >= 0.9 else "warn",
                    "review_classify.coverage",
                )
            )

        if pending:
            insights.append(
                insight(
                    f"还有 {pending} 条差评机器分不出来，已标记「待人工确认」，"
                    f"不计入归因占比也不生成动作 —— 猜错分类比不分类更糟，"
                    f"它会让你去改一个不存在的问题。",
                    "warn",
                    "review_classify.pending",
                )
            )

        low_conf = cls["rule_low"]
        if low_conf:
            insights.append(
                insight(
                    f"{low_conf} 条命中单一关键词、置信度偏低，建议抽查确认。"
                    f"典型误判：用户写「以为续航长」，字面命中「续航」会被归到参数不符，"
                    f"实际是误解型 —— 这两种的补救动作完全不同。",
                    "info",
                    "review_classify.rule_low",
                )
            )

        if agg["bad_count"]:
            insights.append(
                insight(
                    f"差评中 {agg['fixable_count']} 条属于「改文案/补图就能消掉」的类型，"
                    f"可修复率 {fmt_pct(agg['fixable_rate'])}——这部分几乎零成本，"
                    f"剩下 {agg['bad_count'] - agg['fixable_count']} 条才是供应链要处理的。",
                    "good" if agg["fixable_rate"] >= cfg.get("review.fixable_rate_target") else "warn",
                    "review_aggregate.fixable_rate",
                )
            )

            top = agg["bad_by_category"][0]
            insights.append(
                insight(
                    f"最大归因是「{top['category']}」，占差评 {fmt_pct(top['share'])}（{top['count']} 条）。"
                    + ("这类属于认知错配，改详情页即可。" if top["fixable"] else "这类属于履约/质量问题，内容层面修不掉。"),
                    "warn",
                    "bad_by_category[0]",
                )
            )

        # ---- 趋势：近 N 天 vs 前 N 天 ----
        platform = get_platform(payload.get("platform"))
        # 评论的窗口单独配：一周只有几条评论，7 天窗口做比率对比全是噪声
        window = cfg.get_int("timeline.review_window_days")
        tl = timeline.analyze(
            reviews,
            window=window,
            order=platform.date_order,
            # 评论不像广告，不是「每天都该有数据」；缺一天不代表漏导。
            # 所以这里不看覆盖率，看两个窗口里的评论条数够不够。
            min_coverage=0.0,
            series_fn=_day_point,
        )
        if tl["available"]:
            # 一天只有几条评论，按天算的差评率会在 0% 和 33% 之间跳，全是锯齿
            tl["series"] = _smooth_series(tl["series"], span=7)
        trend = _build_trend(tl) if tl["available"] else None

        if trend:
            cur_agg, prev_agg = trend["current"], trend["previous"]
            if trend["enough_sample"]:
                bad_row = next(r for r in trend["rows"] if r["key"] == "bad_rate")
                pts = (bad_row["curr"] - bad_row["prev"]) * 100
                move = "上升" if pts > 0.5 else "下降" if pts < -0.5 else "基本持平"
                insights.append(
                    insight(
                        f"近 {window} 天差评率 {fmt_pct(bad_row['curr'])}（{cur_agg['bad']} 条），"
                        f"前 {window} 天 {fmt_pct(bad_row['prev'])}（{prev_agg['bad']} 条）→ {move} {abs(pts):.1f} 个百分点。"
                        f"样本量够，这个方向可信。",
                        "risk" if pts > 0.5 else "good" if pts < -0.5 else "info",
                        "timeline.change.bad_rate",
                    )
                )
            else:
                insights.append(
                    insight(
                        f"近 {window} 天只有 {cur_agg['bad']} 条差评（前 {window} 天 {prev_agg['bad']} 条），"
                        f"低于 {trend['min_sample']} 条的判断线：比率上的一点波动就是噪声，"
                        f"所以不做「在变好/变坏」的结论 —— 要么拉长窗口，要么再攒几天。",
                        "info",
                        "timeline.sample",
                    )
                )

            risers = trend["risers"]
            if risers and trend["enough_sample"]:
                top = risers[0]
                insights.append(
                    insight(
                        f"「{top['category']}」最近才冒出来：近 {window} 天 {top['curr']} 条，"
                        f"前 {window} 天 {top['prev']} 条。"
                        f"这类「突然出现」的问题通常来自某个环节的变化（换包装、换供应商、改了详情页），"
                        f"比长期存在的问题更值得马上查。",
                        "risk",
                        "timeline.risers[0]",
                    )
                )

            if tl.get("assumption"):
                insights.append(insight(tl["assumption"], "info", "timeline.assumption"))
        else:
            insights.append(
                insight(
                    f"没有做趋势对比：{tl['reason']}。"
                    f"当前结论基于全部评论的平均水平 —— 好评率在爬升还是在下滑，看不出来。",
                    "info",
                    "timeline.unavailable",
                )
            )

        if agg["with_image_rate"] < 0.15:
            insights.append(
                insight(
                    f"带图评论仅 {fmt_pct(agg['with_image_rate'])}：买家秀稀缺会拉低转化，"
                    f"可在包裹卡上做「晒图返券」，但不得诱导好评。",
                    "info",
                    "review_aggregate.with_image_rate",
                )
            )

        if agg["unlabeled_bad"]:
            insights.append(
                insight(
                    f"有 {agg['unlabeled_bad']} 条差评未标注归因，无法进入动作池。"
                    f"先补标注再谈改进，否则改进对象是模糊的。",
                    "warn",
                    "review_aggregate.unlabeled_bad",
                )
            )

        # ---- 动作：每条可修复归因对应一个具体改动 ----
        for item in agg["bad_by_category"]:
            cat = item["category"]
            if not item["fixable"]:
                continue
            fix_target, fix_how, back_to = FIX_PLAYBOOK.get(cat, ("详情页", "补充说明", "listing"))
            actions.append(
                action(
                    "P0" if item["share"] >= cfg.get("review.p0_share") else "P1",
                    fix_target,
                    fix_how,
                    f"该类差评 {item['count']} 条，占 {fmt_pct(item['share'])}，且属于内容可修复类型",
                    f"改版后 30 天内「{cat}」差评占比是否下降一半",
                    "中" if cat in ("使用困难", "描述不符") else "低",
                )
            )
            if back_to:
                feedbacks.append(
                    feedback(back_to, f"「{cat}」差评占 {fmt_pct(item['share'])}，内容未覆盖该预期", fix_target)
                )

        unfixable = [i for i in agg["bad_by_category"] if not i["fixable"]]
        for item in unfixable:
            fix_target, fix_how, _ = FIX_PLAYBOOK.get(item["category"], ("供应链", "人工评估", ""))
            actions.append(
                action(
                    "P1",
                    fix_target,
                    fix_how,
                    f"「{item['category']}」{item['count']} 条，属于内容不可修复类",
                    f"90 天内「{item['category']}」差评数量是否下降",
                    "高",
                )
            )

        headline = (
            f"{agg['total']} 条评论 / 好评率 {fmt_pct(agg['good_rate'])}，"
            f"差评 {agg['bad_count']} 条其中 {fmt_pct(agg['fixable_rate'])} 可内容修复"
        )
        if trend and trend["enough_sample"]:
            bad_row = next(r for r in trend["rows"] if r["key"] == "bad_rate")
            headline += (
                f"；近 {window} 天差评率 {fmt_pct(bad_row['curr'])}"
                f"（前 {window} 天 {fmt_pct(bad_row['prev'])}）"
            )

        return StageResult(
            stage=self.key,
            title=self.title,
            status="ok",
            headline=headline,
            metrics={
                "aggregate": agg,
                "playbook": FIX_PLAYBOOK,
                "classify": cls,
                "timeline": _public_timeline(tl, trend),
            },
            insights=insights,
            actions=actions,
            feedback=feedbacks,
            evidence=evidence,
            next_stage=None,
        )


def _build_trend(tl: dict) -> dict:
    """评论趋势。样本门槛比广告严 —— 评论一天才几条，比率极不稳定。"""
    MIN_SAMPLE = 10

    cur_rows = tl["current"]["rows"]
    prev_rows = tl["previous"]["rows"]
    cur = M.review_aggregate(cur_rows) if cur_rows else None
    prev = M.review_aggregate(prev_rows) if prev_rows else None

    if not cur or not prev:
        return {
            "available": False,
            "enough_sample": False,
            "min_sample": MIN_SAMPLE,
            "current": {"bad": cur["bad"] if cur else 0, "total": cur["total"] if cur else 0},
            "previous": {"bad": prev["bad"] if prev else 0, "total": prev["total"] if prev else 0},
            "rows": [],
            "risers": [],
            "series": tl.get("series", []),
        }

    specs = [
        ("bad_rate", "差评率", "down"),
        ("good_rate", "好评率", "up"),
        ("avg_rating", "平均分", "up"),
    ]
    rows = []
    for key, label, direction in specs:
        before = float(prev[key] or 0)
        after = float(cur[key] or 0)
        delta = after - before
        if abs(delta) < 1e-9:
            move = "same"
        elif (delta > 0) == (direction == "up"):
            move = "better"
        else:
            move = "worse"
        rows.append(
            {
                "key": key,
                "label": label,
                "prev": round(before, 4),
                "curr": round(after, 4),
                "delta": round(delta, 4),
                "delta_pct": round(delta / before, 4) if before else 0.0,
                "trend": move,
            }
        )

    # 类别迁移：只看「变多」的，变少的说明已经在修
    cur_cat = {i["category"]: i["count"] for i in cur["bad_by_category"]}
    prev_cat = {i["category"]: i["count"] for i in prev["bad_by_category"]}
    risers = [
        {
            "category": cat,
            "prev": prev_cat.get(cat, 0),
            "curr": count,
            "delta": count - prev_cat.get(cat, 0),
        }
        for cat, count in cur_cat.items()
        if count - prev_cat.get(cat, 0) > 0 and count >= 3
    ]
    risers.sort(key=lambda r: r["delta"], reverse=True)

    return {
        "available": True,
        "enough_sample": cur["bad"] >= MIN_SAMPLE and prev["bad"] >= MIN_SAMPLE,
        "min_sample": MIN_SAMPLE,
        "current": {"bad": cur["bad"], "total": cur["total"]},
        "previous": {"bad": prev["bad"], "total": prev["total"]},
        "rows": rows,
        "risers": risers[:3],
        "series": tl.get("series", []),
    }


def _day_point(day_rows: list[dict]) -> dict:
    """趋势图上的一个点：当天的差评率、评论量、平均分。

    同时给出 bad / count，让上层能算滚动比率 —— 单日比率没法直接画。
    """
    agg = M.review_aggregate(day_rows)
    return {
        "bad_rate": round(agg["bad_rate"], 4),
        "bad": agg["bad"],
        "count": agg["total"],
        "avg": agg["avg_rating"],
    }


def _smooth_series(series: list[dict], span: int = 7) -> list[dict]:
    """把每日差评率换成「近 span 天滚动差评率」。

    为什么必须滚动：一天 5-8 条评论，按天算的差评率不是 0% 就是 33%，
    折线图看起来像电锯 —— 有锯齿，没有趋势。
    滚动之后才看得出「在变好还是变坏」，这本来就是按月看评论的意义。
    """
    out: list[dict] = []
    for i, point in enumerate(series):
        window = series[max(0, i - span + 1): i + 1]
        total = sum(int(w.get("count", 0) or 0) for w in window)
        bad = sum(int(w.get("bad", 0) or 0) for w in window)
        item = dict(point)
        # 滚动窗口里评论太少时不给点 —— 画一个基于 2 条评论的比率没有意义
        if total >= 10:
            item["bad_rate"] = round(bad / total, 4)
            item["has_data"] = True
        else:
            item["has_data"] = False
        out.append(item)
    return out


def _public_timeline(tl: dict, trend: dict | None) -> dict:
    """整理成 JSON 形状。原始评论行不带出去 —— 那是几百条文本。"""
    def meta(window: dict | None) -> dict | None:
        if not window:
            return None
        return {
            key: window[key]
            for key in ("start", "end", "row_count", "coverage", "days_with_data")
        }

    return {
        "available": tl["available"],
        "reason": tl["reason"],
        "assumption": tl.get("assumption", ""),
        "window_days": tl["window_days"],
        "range": tl.get("range"),
        "current": meta(tl.get("current")),
        "previous": meta(tl.get("previous")),
        "series": tl.get("series", []),
        "change": (trend or {}).get("rows", []),
        "risers": (trend or {}).get("risers", []),
        "enough_sample": (trend or {}).get("enough_sample", False),
        "min_sample": (trend or {}).get("min_sample", 10),
        "smooth_days": 7,
    }
