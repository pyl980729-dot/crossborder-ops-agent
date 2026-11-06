"""02 选品与定价 Agent —— 把「卖多少钱」变成一道有唯一答案的算术题。

这个 Agent 只做三件事：
1. 按费率结构算清贡献毛利（多少钱是真正赚的）
2. 反算盈亏线 ROAS（广告花到多少就开始白干）
3. 给出 SKU 四角色结构（每个角色承担不同利润职责）

它不做的事：不猜转化率、不假设税率、不给「建议售价 ±20%」这种没用的区间。
"""

from __future__ import annotations

from app.agents.base import StageAgent, action, fmt_money, fmt_pct, insight
from app.core import metrics as M
from app.core.contracts import StageResult
from app.core.platforms import get_platform


class StrategyAgent(StageAgent):
    key = "strategy"

    def _run(self, payload: dict) -> StageResult:
        platform = get_platform(payload.get("platform"))
        price = float(payload["target_price"])
        cost = float(payload["product_cost"])

        commission = float(payload.get("commission_rate", platform.commission_rate))
        tax = float(payload.get("tax_rate", platform.tax_rate))
        return_rate = float(payload.get("return_rate", 0.02))
        shipping = float(payload.get("shipping_cost", 0.0))
        target_margin = float(payload.get("target_margin", 0.2))

        model = M.pricing_model(
            price=price,
            product_cost=cost,
            commission_rate=commission,
            tax_rate=tax,
            return_rate=return_rate,
            shipping_cost=shipping,
            target_margin=target_margin,
        )
        skus = M.sku_matrix(
            base_cost=cost,
            base_price=price,
            commission_rate=commission,
            tax_rate=tax,
            return_rate=return_rate,
            shipping_cost=shipping,
        )

        insights = []
        actions = []
        evidence = ["metrics.pricing_model", "metrics.sku_matrix"]

        # ---- 净利率体检 ----
        margin = model["net_margin_rate"]
        if margin < 0:
            insights.append(
                insight(
                    f"净利率 {fmt_pct(margin)} 为负：这个价格卖一件亏 {fmt_money(abs(model['contribution']))}。"
                    f"不是广告能救的问题，必须改成本或改价。",
                    "risk",
                    "pricing_model.net_margin_rate",
                )
            )
        elif margin < 0.08:
            insights.append(
                insight(
                    f"净利率仅 {fmt_pct(margin)}：广告稍微跑偏就转亏，属于「薄利必须先跑出自然流量」的结构，"
                    f"前 4 周不建议开大预算。",
                    "warn",
                    "pricing_model.net_margin_rate",
                )
            )
        elif margin < 0.2:
            insights.append(
                insight(
                    f"净利率 {fmt_pct(margin)}：健康区间下沿，可以支撑中等强度投放。",
                    "info",
                    "pricing_model.net_margin_rate",
                )
            )
        else:
            insights.append(
                insight(
                    f"净利率 {fmt_pct(margin)}：结构健康，单件贡献 {fmt_money(model['contribution'])}，"
                    f"足以支撑测试期亏损换排名。",
                    "good",
                    "pricing_model.net_margin_rate",
                )
            )

        # ---- 盈亏线 ----
        if model["breakeven_roas"] > 0:
            insights.append(
                insight(
                    f"盈亏线 ROAS = {model['breakeven_roas']}（ACoS {fmt_pct(model['breakeven_acos'])}）："
                    f"广告 ROAS 低于这个数，每一单都在用利润补贴平台。"
                    f"可承受 CPC 约 {fmt_money(model['affordable_cpc'])}。",
                    "info",
                    "pricing_model.breakeven_roas",
                )
            )

        # ---- 达标价与促销空间 ----
        required = model["required_price"]
        if required and price < required:
            gap = (required - price) / price
            insights.append(
                insight(
                    f"要在当前成本结构下拿到 {fmt_pct(target_margin, 0)} 净利率，售价至少要 "
                    f"{fmt_money(required)}，当前 {fmt_money(price)} 还差 {fmt_pct(gap)}"
                    f"——要么提价，要么把成本压到 {fmt_money(price * (1 - gap * 0.6))} 以下。",
                    "risk",
                    "pricing_model.required_price",
                )
            )
        elif required:
            insights.append(
                insight(
                    f"当前售价 {fmt_money(price)} 高于达标线 {fmt_money(required)} 约 "
                    f"{fmt_pct(model['headroom_rate'])}：这段空间是优惠券/大促的弹药，"
                    f"不要直接降价——直接降价会把价格锚点打穿，券能收回来。",
                    "good",
                    "pricing_model.headroom_rate",
                )
            )

        # ---- SKU 结构 ----
        thin = [s for s in skus if s["net_margin_rate"] < 0.05]
        if thin:
            insights.append(
                insight(
                    "SKU 结构中 " + "、".join(f"{s['role']}（{fmt_pct(s['net_margin_rate'])}）" for s in thin)
                    + " 净利率低于 5%，这些款只能作为流量入口，不能计入利润预算。",
                    "warn",
                    "sku_matrix.net_margin_rate",
                )
            )

        headline = (
            f"售价 {fmt_money(price)} / 成本 {fmt_money(cost)}：净利率 {fmt_pct(margin)}，"
            f"盈亏线 ROAS {model['breakeven_roas']}，可承受 CPC {fmt_money(model['affordable_cpc'])}"
        )

        # ---- 动作 ----
        if margin < 0.08:
            actions.append(
                action(
                    "P0",
                    "定价",
                    f"先提价至 {fmt_money(model['required_price'] or price * 1.12)} 或压成本 10% 以上",
                    f"当前净利率 {fmt_pct(margin)} 不足以支撑任何投放测试",
                    "改后重跑本模块，净利率 ≥ 12% 才开广告",
                    "中",
                )
            )
        actions.append(
            action(
                "P0",
                "广告预算上限",
                f"按盈亏线 ROAS {model['breakeven_roas']} 设定硬止损，低于 0.7× 立即降预算",
                "没有止损线的投放等于把定价权交给算法",
                "每周复盘实际 ROAS 是否稳定高于盈亏线",
            )
        )
        actions.append(
            action(
                "P1",
                "SKU 结构",
                "按四角色上架：引流款压价拉新、主推款承担利润、高配款提客单、套装款摊物流",
                "单一价格点无法同时满足拉新和利润两个目标",
                "30 天后看主推款销量占比是否 >50%",
            )
        )

        return StageResult(
            stage=self.key,
            title=self.title,
            status="ok",
            headline=headline,
            metrics={
                "pricing": model,
                "skus": skus,
                "platform": {"key": platform.key, "name": platform.name},
            },
            insights=insights,
            actions=actions,
            evidence=evidence,
            next_stage="listing",
        )
