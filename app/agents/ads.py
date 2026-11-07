"""06 广告诊断 Agent —— 只出诊断，不碰后台。

这是整套系统里唯一会产生「钱」的模块，所以它守三条铁律：
1. 不执行：绝不自动改预算/出价，动作清单交人审批
2. 不猜数：所有指标走 metrics，Agent 只解读分级
3. 不暂停：止血手段是降预算降出价，暂停会打断学习期、拉低权重

诊断顺序固定不可跳步：数据充分性 → 有无花费 → 点击率 → 零单 → ROAS 分级。
顺序错了会把噪声当结论，动作做反。
"""

from __future__ import annotations

from app.agents.base import StageAgent, action, feedback, fmt_money, fmt_pct, insight
from app.core import config, metrics as M, timeline
from app.core.contracts import StageResult
from app.core.platforms import get_platform


class AdsAgent(StageAgent):
    key = "ads"

    def _run(self, payload: dict) -> StageResult:
        platform = get_platform(payload.get("platform"))
        # 阈值按「请求 > 界面设置 > 平台预设 > 系统默认」四层合成
        cfg = config.load_config(payload.get("platform"), payload.get("thresholds"))
        price = float(payload["target_price"])
        cost = float(payload["product_cost"])
        commission = float(payload.get("commission_rate", platform.commission_rate))
        tax = float(payload.get("tax_rate", platform.tax_rate))
        # 保留 payload.ctr_bench 的旧写法，但默认值来自配置层
        ctr_bench = float(payload.get("ctr_bench", cfg.get("ads.ctr_bench")))
        target_margin = float(payload.get("target_margin", 0.10))
        shipping = float(payload.get("shipping_cost", 0.0))

        # 成本口径必须与 02 完全一致，否则同一次运行里会出现两个盈亏线
        model = M.pricing_model(
            price=price,
            product_cost=cost,
            commission_rate=commission,
            tax_rate=tax,
            shipping_cost=shipping,
            target_margin=target_margin,
        )
        breakeven = model["breakeven_roas"]
        contribution = model["contribution"]
        # 目标 ROAS：扣掉广告费后还能留下 target_margin 的净利
        target_roas = round(price / (contribution - price * target_margin), 2) if contribution - price * target_margin > 0 else breakeven * 1.3

        rows = []
        for c in payload["campaigns"]:
            row = dict(c)
            row.setdefault("price", price)
            rows.append(row)

        # ---- 时间轴：本期 vs 上期（同长度窗口）----
        window = cfg.get_int("timeline.ads_window_days")
        tl = timeline.analyze(
            rows,
            window=window,
            order=platform.date_order,
            min_coverage=cfg.get("timeline.min_coverage"),
            series_fn=_day_point,
        )
        # 有日期就用「近 N 天」诊断。拿 90 天的平均值做今天的决策，
        # 会把上周刚开始失血的组平均成一个「还行」，等发现时已经亏了两周。
        scope_rows = tl["current"]["rows"] if tl["available"] else rows
        scope_label = f"近 {window} 天" if tl["available"] else "全表汇总"

        report = M.diagnose_ads(
            scope_rows,
            breakeven_roas=breakeven,
            target_roas=target_roas,
            ctr_bench=ctr_bench,
            min_impressions=cfg.get_int("ads.min_impressions"),
            min_clicks=cfg.get_int("ads.min_clicks"),
            bleeding_ratio=cfg.get("ads.bleeding_ratio"),
        )
        summary = report["summary"]
        campaigns = report["campaigns"]

        p0 = [c for c in campaigns if c["grade"]["level"] == "P0"]
        p1 = [c for c in campaigns if c["grade"]["level"] == "P1"]
        p2 = [c for c in campaigns if c["grade"]["level"] == "P2"]

        insights = []
        actions = []
        feedbacks = []
        evidence = [
            "metrics.pricing_model",
            "metrics.diagnose_ads",
            "campaigns[*].grade",
            "timeline.analyze",
        ]

        # ---- 整体盈亏 ----
        net = summary["net_after_ads"]
        insights.append(
            insight(
                f"{scope_label}广告花费 {fmt_money(summary['total_cost'])}，带回 {fmt_money(summary['total_revenue'])}，"
                f"整体 ROAS {summary['overall_roas']}（盈亏线 {breakeven}）→ "
                + (f"净{'赚' if net >= 0 else '亏'} {fmt_money(abs(net))}")
                + f"，整体 CPA {fmt_money(summary['overall_cpa'])}，客单价 {fmt_money(price)}。",
                "good" if net >= 0 else "risk",
                "summary.net_after_ads",
            )
        )

        # ---- 趋势：本期 vs 上期 ----
        grade_kwargs = {
            "breakeven_roas": breakeven,
            "target_roas": target_roas,
            "ctr_bench": ctr_bench,
            "min_impressions": cfg.get_int("ads.min_impressions"),
            "min_clicks": cfg.get_int("ads.min_clicks"),
            "bleeding_ratio": cfg.get("ads.bleeding_ratio"),
        }
        trend = (
            self._trend(tl, window, cfg.get("timeline.mover_threshold"), grade_kwargs)
            if tl["available"]
            else None
        )

        if trend:
            by_key = {r["key"]: r for r in trend["rows"]}
            roas_row = by_key["overall_roas"]
            p0_row = by_key["p0_count"]
            level = {"better": "good", "worse": "risk", "same": "info"}[roas_row["trend"]]
            verb = "上升" if roas_row["delta"] > 0 else "下降" if roas_row["delta"] < 0 else "持平"
            insights.append(
                insight(
                    f"整体 ROAS {_fmt_change(roas_row)}（{verb} {fmt_pct(abs(roas_row['delta_pct']))}），"
                    f"同期花费 {_fmt_change(by_key['total_cost'])}，"
                    f"订单 {_fmt_change(by_key['total_orders'])}。"
                    f"这是「近 {window} 天 vs 前 {window} 天」的同长度对比，不是自然周。",
                    level,
                    "timeline.change.roas",
                )
            )

            # P0 组数变化是闭环最硬的证据：上次让止血，止住了没有
            if p0_row["delta"] != 0:
                insights.append(
                    insight(
                        f"P0 失血组 {int(p0_row['prev'])} 组 → {int(p0_row['curr'])} 组"
                        + ("，止血生效。" if p0_row["delta"] < 0 else "，还在增加，止血动作没执行或没到位。")
                        + "这一条比 ROAS 更能说明「上期的动作有没有用」，因为它数的是「还在倒贴的组」。",
                        "good" if p0_row["delta"] < 0 else "risk",
                        "timeline.change.p0",
                    )
                )

            worse = [c for c in trend["campaigns"] if c["trend"] == "worse"]
            better = [c for c in trend["campaigns"] if c["trend"] == "better"]
            if worse:
                detail = "、".join(
                    f"{c['name']} {c['prev']} → {c['curr']}"
                    + (f"（{c['note']}）" if c.get("note") else "")
                    for c in worse[:3]
                )
                insights.append(
                    insight(
                        f"{len(worse)} 个组在恶化：{detail}。"
                        f"整体 ROAS 由好转坏会被健康组掩盖，逐组看才知道该动哪个。",
                        "risk",
                        "timeline.campaigns.worse",
                    )
                )
            if better:
                detail = "、".join(f"{c['name']} {c['prev']} → {c['curr']}" for c in better[:3])
                insights.append(
                    insight(
                        f"{len(better)} 个组在改善：{detail}。"
                        f"先确认改动只动了一个变量，再看能不能复制到同类组。",
                        "good",
                        "timeline.campaigns.better",
                    )
                )

            cur_cov, prev_cov = tl["current"]["coverage"], tl["previous"]["coverage"]
            if cur_cov < 1 or prev_cov < 1:
                insights.append(
                    insight(
                        f"覆盖率不完整：本期 {tl['current']['days_with_data']}/{window} 天、"
                        f"上期 {tl['previous']['days_with_data']}/{window} 天有数据。"
                        f"如果缺的那几天是「没投放」而非「漏导」，对比仍然成立；"
                        f"如果是漏导，涨跌会被低估。",
                        "warn",
                        "timeline.coverage",
                    )
                )

            if tl.get("assumption"):
                insights.append(insight(tl["assumption"], "info", "timeline.assumption"))
        else:
            insights.append(
                insight(
                    f"没有做趋势对比：{tl['reason']}。"
                    f"当前按整表汇总诊断，结论反映这段数据的平均水平，"
                    f"看不出「最近是在变好还是变坏」—— 要判断趋势，导出时请带上日期列。",
                    "warn",
                    "timeline.unavailable",
                )
            )

        if summary["p0_count"]:
            insights.append(
                insight(
                    f"有 {summary['p0_count']} 个广告组进入 P0（失血）："
                    f"{'、'.join(c['campaign'] for c in p0)}。"
                    f"这类组的特点不是「效果差」，而是「每出一单都在倒贴现金」，优先级高于任何优化动作。",
                    "risk",
                    "summary.p0_count",
                )
            )

        if p1:
            insights.append(
                insight(
                    f"{len(p1)} 个组处于 P1（可修）：问题集中在"
                    f"{self._dominant_stage(p1)}，改的是环节不是预算。",
                    "warn",
                    "campaigns.grade.stage",
                )
            )

        if p2:
            insights.append(
                insight(
                    f"{len(p2)} 个组已达目标 ROAS（{target_roas}），可以放量："
                    f"{'、'.join(c['campaign'] for c in p2)}。放量节奏建议每次 +20% 预算，不要翻倍。",
                    "good",
                    "campaigns.grade.level",
                )
            )

        # ---- 整体漏斗体检 ----
        if summary["overall_ctr"] < ctr_bench:
            insights.append(
                insight(
                    f"整体 CTR {fmt_pct(summary['overall_ctr'])} 低于基准 {fmt_pct(ctr_bench)}："
                    f"这是创意问题不是出价问题——加价只会用更贵的价格买到同样没人点的位置。",
                    "warn",
                    "summary.overall_ctr",
                )
            )
            feedbacks.append(
                feedback("image", "CTR 全面低于基准，主图/标题吸引力不足", "重做主图第 1-2 张，一版只改一个变量")
            )

        if summary["overall_cvr"] < 0.01 and summary["total_clicks"] > 100:
            insights.append(
                insight(
                    f"整体 CVR 仅 {fmt_pct(summary['overall_cvr'])}：点进来的人不买，"
                    f"问题在详情页与评价，不在流量质量。",
                    "warn",
                    "summary.overall_cvr",
                )
            )
            feedbacks.append(
                feedback("listing", "点击有量但转化极低，详情页未承接搜索意图", "补「注意事项」段与包装清单，检查价格竞争力")
            )

        # ---- 动作清单：按分级给，不给笼统建议 ----
        for c in p0[:5]:
            actions.append(
                action(
                    "P0",
                    c["campaign"],
                    f"预算下调 30%-50%（当天已花费则次日执行），出价下调 15%",
                    c["grade"]["reason"],
                    "3 天后 ROAS 回到盈亏线以上；未回到则关停该组",
                )
            )
        if p0:
            feedbacks.append(
                feedback("listing", f"{len(p0)} 个组疑似落地页不支撑", "先修详情页与价格，再恢复投放")
            )

        for c in p1[:5]:
            actions.append(
                action(
                    "P1",
                    c["campaign"],
                    "维持预算，先改创意/落地页；7 天后再判断是否降出价",
                    c["grade"]["reason"],
                    "7 天后 CTR 或 CVR 是否改善 20% 以上",
                    "中",
                )
            )

        for c in p2[:3]:
            actions.append(
                action(
                    "P2",
                    c["campaign"],
                    "预算 +20%，观察 3 天再决定是否继续加",
                    c["grade"]["reason"],
                    "加预算后 ROAS 是否仍 ≥ 目标值",
                )
            )

        if not p0 and not p1:
            actions.append(
                action("P2", "整体", "全组健康，按 +20% 节奏放量", "无失血组，可扩大测试面", "放量后 ROAS 不跌破目标")
            )

        headline = (
            f"{scope_label}：花费 {fmt_money(summary['total_cost'])} / "
            f"收入 {fmt_money(summary['total_revenue'])}，"
            f"整体 ROAS {summary['overall_roas']} vs 盈亏线 {breakeven} → "
            f"P0 {len(p0)} 组 / P1 {len(p1)} 组 / P2 {len(p2)} 组"
        )

        return StageResult(
            stage=self.key,
            title=self.title,
            status="ok",
            headline=headline,
            metrics={
                "summary": summary,
                "campaigns": campaigns,
                "thresholds": {
                    "breakeven_roas": breakeven,
                    "target_roas": target_roas,
                    "ctr_bench": ctr_bench,
                    "contribution": contribution,
                    "min_impressions": cfg.get_int("ads.min_impressions"),
                    "min_clicks": cfg.get_int("ads.min_clicks"),
                    "bleeding_ratio": cfg.get("ads.bleeding_ratio"),
                    "ctr_bench_source": cfg.source_label("ads.ctr_bench"),
                },
                "platform": {"key": platform.key, "name": platform.name},
                "timeline": _public_timeline(tl, trend),
            },
            insights=insights,
            actions=actions,
            feedback=feedbacks,
            evidence=evidence,
            next_stage="review",
        )

    @staticmethod
    def _trend(tl: dict, window: int, mover_threshold: float, grade_kwargs: dict) -> dict:
        """本期 vs 上期的指标涨跌 + 逐组变化 + 分级迁移。

        方向的定义要小心：花费上升既不是好也不是坏（ROAS 才是），
        所以给它 flat —— 强行给它一个方向会误导。
        """
        cur = M.ads_totals(tl["current"]["rows"])
        prev = M.ads_totals(tl["previous"]["rows"])

        # 上期也用同一套阈值分一次级：不然「P0 从 2 组降到 1 组」这种
        # 最硬的闭环证据就拿不到 —— 那正是「上次让他止血，他止了没有」的答案
        prev_report = M.diagnose_ads(tl["previous"]["rows"], **grade_kwargs)
        prev_levels = {c["campaign"]: c["grade"]["level"] for c in prev_report["campaigns"]}
        cur_report = M.diagnose_ads(tl["current"]["rows"], **grade_kwargs)
        cur_levels = {c["campaign"]: c["grade"]["level"] for c in cur_report["campaigns"]}

        specs = [
            ("p0_count", "P0 失血组", "down", "int"),
            ("overall_roas", "整体 ROAS", "up", "num"),
            ("total_cost", "广告花费", "flat", "money"),
            ("total_orders", "订单数", "up", "int"),
            ("overall_ctr", "整体 CTR", "up", "pct"),
            ("overall_cvr", "转化率", "up", "pct"),
            ("overall_cpa", "CPA", "down", "money"),
        ]

        rows = []
        for key, label, direction, fmt in specs:
            if key == "p0_count":
                before = float(prev_report["summary"]["p0_count"])
                after = float(cur_report["summary"]["p0_count"])
            else:
                before = float(prev.get(key) or 0)
                after = float(cur.get(key) or 0)
            delta = after - before
            delta_pct = round(delta / before, 4) if before else 0.0

            # 比率类指标给 1% 的相对死区：CTR 从 3.440% 变到 3.442% 不是趋势，是噪声。
            # 不加死区，报告里每天都会出现「CTR 上升」这种没意义的结论。
            noise_band = 0.01 if fmt == "pct" else 0.0
            if abs(delta) < 1e-9 or direction == "flat" or abs(delta_pct) < noise_band:
                move = "same"
            elif (delta > 0) == (direction == "up"):
                move = "better"
            else:
                move = "worse"

            rows.append(
                {
                    "key": key,
                    "label": label,
                    "fmt": fmt,
                    "direction": direction,
                    "prev": round(before, 4),
                    "curr": round(after, 4),
                    "delta": round(delta, 4),
                    "delta_pct": delta_pct,
                    "trend": move,
                }
            )

        changes = timeline.group_delta(
            tl["current"]["rows"],
            tl["previous"]["rows"],
            key="campaign",
            value_fn=lambda rs: M.ads_totals(rs)["overall_roas"],
        )
        breakeven = grade_kwargs["breakeven_roas"]
        for c in changes:
            c["level_prev"] = prev_levels.get(c["name"])
            c["level_curr"] = cur_levels.get(c["name"])
            if c["prev"] >= breakeven > c["curr"]:
                c["note"] = f"已跌破盈亏线 {breakeven}"
            elif c["prev"] < breakeven <= c["curr"]:
                c["note"] = f"已回到盈亏线 {breakeven} 以上"
            else:
                c["note"] = ""

            if c["prev"] <= 0 and c["curr"] <= 0:
                # 两期都零单：ROAS 无意义，但这是「一直没起量」，不是「持平」
                c["trend"] = "flat_zero"
            elif c["delta_pct"] <= -mover_threshold:
                c["trend"] = "worse"
            elif c["delta_pct"] >= mover_threshold:
                c["trend"] = "better"
            else:
                c["trend"] = "same"

        changes.sort(key=lambda c: c["delta_pct"])
        return {"rows": rows, "campaigns": changes, "window_days": window}


    @staticmethod
    def _dominant_stage(campaigns: list[dict]) -> str:
        from collections import Counter

        labels = {
            "click_through": "点击率（创意层面）",
            "click_no_order": "落地页转化（详情页/价格）",
            "bleeding": "投放结构失血（出价/选词）",
            "below_breakeven": "转化效率不足",
        }
        counter = Counter(c["grade"]["stage"] for c in campaigns)
        top = counter.most_common(1)[0][0]
        return labels.get(top, top)


# ---------------------------------------------------------------------------
# 时间轴辅助
# ---------------------------------------------------------------------------

def _fmt_change(row: dict) -> str:
    """按指标类型格式化「前 → 后」，别让订单数显示成 220.0。"""
    kind = row.get("fmt")
    if kind == "int":
        return f"{int(row['prev'])} → {int(row['curr'])}"
    if kind == "money":
        return f"{fmt_money(row['prev'])} → {fmt_money(row['curr'])}"
    if kind == "pct":
        return f"{fmt_pct(row['prev'])} → {fmt_pct(row['curr'])}"
    return f"{row['prev']} → {row['curr']}"


def _day_point(day_rows: list[dict]) -> dict:
    """趋势图上的一个点。

    字段刻意只留 4 个：30 天 × 全量字段会明显撑大响应，
    而前端画图只需要这几个。
    """
    t = M.ads_totals(day_rows)
    return {
        "roas": t["overall_roas"],
        "cost": t["total_cost"],
        "orders": t["total_orders"],
        "ctr": round(t["overall_ctr"], 4),
    }


def _public_timeline(tl: dict, trend: dict | None) -> dict:
    """整理成可放进 JSON 的形状。

    关键是不能把 current.rows / previous.rows 带出去 ——
    那是几十天的原始广告行，前端一个字都用不上，却会让响应大十几倍。
    """
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
        "rows_without_date": tl.get("rows_without_date", 0),
        "current": meta(tl.get("current")),
        "previous": meta(tl.get("previous")),
        "series": tl.get("series", []),
        "change": trend["rows"] if trend else [],
        "campaigns": trend["campaigns"] if trend else [],
    }
