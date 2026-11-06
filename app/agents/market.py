"""01 市场调研 Agent —— 回答一个问题：这个类目该不该进，从哪个价位切。

思路（也是它比"AI 选品"靠谱的地方）：
不问模型「这个类目好不好」，而是先算容量、集中度、价格带供需比，
让模型只做一件事——把算出来的数字翻译成一句人话结论。
"""

from __future__ import annotations

from app.agents.base import StageAgent, action, fmt_money, fmt_pct, insight
from app.core import metrics as M
from app.core.contracts import StageResult
from app.core.platforms import get_platform


class MarketAgent(StageAgent):
    key = "market"

    def _run(self, payload: dict) -> StageResult:
        products = payload["products"]
        category = payload["category"]
        platform = get_platform(payload.get("platform"))

        summary = M.market_summary(products)
        conc = M.concentration(products, key="brand")
        bands = M.price_bands(products)
        configs = M.config_opportunity(products)

        blue = next((b for b in bands if b.get("tag") == "blue_ocean"), None)
        red = next((b for b in bands if b.get("tag") == "red_ocean"), None)

        insights = []
        actions = []
        evidence = [
            "metrics.market_summary",
            "metrics.concentration(brand)",
            "metrics.price_bands",
            "metrics.config_opportunity",
        ]

        # ---- 集中度判断 ----
        conc_label = f"CR3 {fmt_pct(conc['cr3'])}" if not conc["cr10_meaningful"] else f"CR10 {fmt_pct(conc['cr10'])}"
        if conc["verdict"] == "高度集中":
            insights.append(
                insight(
                    f"{conc_label}，属于高度集中：头部 {len(conc['top'])} 个品牌吃掉大部分销量，"
                    f"正面打价格战没有胜算，必须靠配置差异或人群差异切细分。",
                    "risk",
                    "concentration.verdict",
                )
            )
        elif conc["verdict"] == "中度集中":
            insights.append(
                insight(
                    f"{conc_label}，中度集中：有头部但不垄断，"
                    f"找准一个未被覆盖的配置组合有机会拿到类目 3%-8% 份额。",
                    "info",
                    "concentration.verdict",
                )
            )
        else:
            insights.append(
                insight(
                    f"{conc_label}，高度分散：没有品牌壁垒，"
                    f"竞争会迅速滑向价格战，进入前必须先把成本结构算清。",
                    "warn",
                    "concentration.verdict",
                )
            )

        # ---- 价格带机会 ----
        if blue:
            gap = blue["sales_share"] - blue["supply_share"]
            insights.append(
                insight(
                    f"价格带 {blue['label']} 供给占比 {fmt_pct(blue['supply_share'])}，"
                    f"却贡献 {fmt_pct(blue['sales_share'])} 的销量（机会指数 {blue['opportunity']}，"
                    f"供需缺口 {fmt_pct(abs(gap))}）——这是类目里唯一「货少需求多」的位置。",
                    "good",
                    f"price_bands[{blue['label']}].opportunity",
                )
            )
        if red:
            insights.append(
                insight(
                    f"价格带 {red['label']} 有 {red['product_count']} 个链接挤在一起，"
                    f"只拿到 {fmt_pct(red['sales_share'])} 销量，机会指数 {red['opportunity']}——红海，不要进。",
                    "warn",
                    f"price_bands[{red['label']}].opportunity",
                )
            )

        # ---- 新品友好度 ----
        if summary["new_seller_share"] < 0.1:
            insights.append(
                insight(
                    f"TOP 商品里新卖家占比仅 {fmt_pct(summary['new_seller_share'])}，"
                    f"说明榜单被老链接把持，冷启动周期会比类目平均值更长，预算要留够 8-12 周。",
                    "warn",
                    "market_summary.new_seller_share",
                )
            )

        # ---- 口碑缺口 ----
        if summary["avg_rating"] < 4.2:
            insights.append(
                insight(
                    f"类目平均评分 {summary['avg_rating']}（低于 4.2），低分占比 {fmt_pct(summary['low_rating_share'])}："
                    f"需求真实存在但现有产品没满足好，这是最便宜的产品机会——把差评里提到的问题修掉就是卖点。",
                    "good",
                    "market_summary.avg_rating",
                )
            )

        # ---- 配置机会 ----
        if configs:
            top_cfg = configs[0]
            insights.append(
                insight(
                    f"卖得最好的配置是「{top_cfg['config']}」：{top_cfg['product_count']} 个链接吃掉 "
                    f"{fmt_pct(top_cfg['sales_share'])} 销量，平均售价 {fmt_money(top_cfg['avg_price'])}。",
                    "info",
                    "config_opportunity[0]",
                )
            )

        # ---- 结论 ----
        verdict = self._verdict(conc, blue, summary)
        headline = (
            f"{category}：月销 {summary['total_monthly_sales']:,} 件 / GMV {fmt_money(summary['monthly_gmv'])}，"
            f"{conc_label}（{conc['verdict']}）"
            + (f"，机会价位 {blue['label']}" if blue else "")
            + f" → {verdict['decision']}"
        )

        # ---- 动作 ----
        if blue:
            actions.append(
                action(
                    "P0",
                    f"价格带 {blue['label']}",
                    f"把首发 SKU 定价锚定在 {blue['label']}（当前带内均价 {fmt_money(blue['avg_price'])}）",
                    f"该带机会指数 {blue['opportunity']}，是类目唯一供不应求的位置",
                    "上架 14 天后看该带内排名是否进入前 30",
                )
            )
        if red:
            actions.append(
                action(
                    "P1",
                    f"价格带 {red['label']}",
                    "主动避开该价位，不做首发的对标款",
                    f"该带 {red['product_count']} 个链接抢 {fmt_pct(red['sales_share'])} 销量，进入即亏损",
                    "开局 30 天不出现该价位 SKU",
                )
            )
        if conc["verdict"] == "高度集中":
            actions.append(
                action(
                    "P0",
                    "产品定义",
                    "放弃参数对标，改做「场景套装 + 差异化配置」",
                    "头部集中度高，同质化对标无法获得搜索权重",
                    "对比首发 30 天点击率是否高于类目均值",
                    "中",
                )
            )
        actions.append(
            action(
                "P1",
                "差评库",
                "抓取类目 TOP10 的所有 1-2 星评论，建问题清单",
                "低评分占比高说明改良空间即卖点空间",
                "问题清单 ≥5 条且每条对应一个可验证的产品改动",
            )
        )

        result = StageResult(
            stage=self.key,
            title=self.title,
            status="ok",
            headline=headline,
            metrics={
                "summary": summary,
                "concentration": conc,
                "price_bands": bands,
                "configs": configs[:6],
                "verdict": verdict,
                "platform": {"key": platform.key, "name": platform.name},
            },
            insights=insights,
            actions=actions,
            evidence=evidence,
            next_stage="strategy",
        )
        return result

    @staticmethod
    def _verdict(conc: dict, blue: dict | None, summary: dict) -> dict:
        """把三个信号收敛成一个可执行结论。"""
        score = 0
        reasons = []
        if conc["verdict"] != "高度集中":
            score += 1
            reasons.append("集中度可控")
        else:
            reasons.append("集中度过高")
        if blue and blue["opportunity"] >= 1.2:
            score += 1
            reasons.append(f"{blue['label']} 供不应求")
        if summary["avg_rating"] < 4.2:
            score += 1
            reasons.append("口碑存在改良缺口")
        if summary["total_monthly_sales"] >= 5000:
            score += 1
            reasons.append("类目容量足够支撑起量")
        else:
            reasons.append("容量偏小")

        if score >= 3:
            decision = "可进，按机会价位差异化切入"
        elif score == 2:
            decision = "谨慎进，先小批量测试 4 周"
        else:
            decision = "不建议进，除非有独特供应链优势"

        return {"decision": decision, "score": score, "reasons": reasons, "max_score": 4}
