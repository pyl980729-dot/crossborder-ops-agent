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
from app.core import config, metrics as M
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

        report = M.diagnose_ads(
            rows,
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
        evidence = ["metrics.pricing_model", "metrics.diagnose_ads", "campaigns[*].grade"]

        # ---- 整体盈亏 ----
        net = summary["net_after_ads"]
        insights.append(
            insight(
                f"本期广告花费 {fmt_money(summary['total_cost'])}，带回 {fmt_money(summary['total_revenue'])}，"
                f"整体 ROAS {summary['overall_roas']}（盈亏线 {breakeven}）→ "
                + (f"净{'赚' if net >= 0 else '亏'} {fmt_money(abs(net))}")
                + f"，整体 CPA {fmt_money(summary['overall_cpa'])}，客单价 {fmt_money(price)}。",
                "good" if net >= 0 else "risk",
                "summary.net_after_ads",
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
            f"花费 {fmt_money(summary['total_cost'])} / 收入 {fmt_money(summary['total_revenue'])}，"
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
            },
            insights=insights,
            actions=actions,
            feedback=feedbacks,
            evidence=evidence,
            next_stage="review",
        )

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
