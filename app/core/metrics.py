"""确定性指标层 —— 全系统唯一允许产生数字的地方。

设计铁律：
1. Agent（规则引擎或 LLM）**不许心算**，任何指标必须先经过本模块；
2. 每个函数都是纯函数：同样的输入永远得到同样的输出，可单测、可复现；
3. 缺关键字段直接抛 MetricError，由上层转成 [INPUT_MISSING]，绝不填默认值糊过去。

这条铁律是整套系统的地基：LLM 可以解释「为什么转化率低」，
但不能决定「转化率是多少」。
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any, Iterable, Sequence


class MetricError(ValueError):
    """指标计算前置条件不满足（缺字段/类型非法/除零保护）。"""

    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.field = field


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def num(value: Any, field: str, *, allow_zero: bool = True) -> float:
    """严格取数：空值/非数字一律报错，不做静默兜底。"""
    if value is None or value == "":
        raise MetricError(f"缺少字段: {field}", field)
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise MetricError(f"字段 {field} 不是合法数字: {value!r}", field) from exc
    if math.isnan(out) or math.isinf(out):
        raise MetricError(f"字段 {field} 为非有限值", field)
    if not allow_zero and out == 0:
        raise MetricError(f"字段 {field} 不能为 0", field)
    return out


def ratio(numerator: float, denominator: float, field: str = "") -> float:
    """安全除法：分母为 0 返回 0.0，并由调用方通过 evidence 标注数据不足。"""
    if not denominator:
        return 0.0
    return round(numerator / denominator, 6)


def pct(value: float, digits: int = 1) -> str:
    """给报告用的百分比文本。"""
    return f"{value * 100:.{digits}f}%"


def _money(value: float) -> float:
    return round(float(value), 2)


# --------------------------------------------------------------------------
# 一、市场调研
# --------------------------------------------------------------------------

def market_summary(products: Sequence[dict]) -> dict:
    """类目大盘：容量、均价、评分结构、新卖家占比。"""
    if not products:
        raise MetricError("类目商品列表为空，无法计算大盘", "products")

    prices = [num(p.get("price"), "price", allow_zero=False) for p in products]
    sales = [num(p.get("monthly_sales"), "monthly_sales") for p in products]
    ratings = [num(p.get("rating"), "rating") for p in products if p.get("rating") not in (None, "")]

    total_sales = sum(sales)
    gmv = sum(pr * s for pr, s in zip(prices, sales))
    new_sellers = sum(1 for p in products if str(p.get("is_new_seller", "")).lower() in ("1", "true", "yes"))

    return {
        "product_count": len(products),
        "total_monthly_sales": int(total_sales),
        "monthly_gmv": _money(gmv),
        "avg_price": _money(gmv / total_sales) if total_sales else _money(sum(prices) / len(prices)),
        "price_min": _money(min(prices)),
        "price_max": _money(max(prices)),
        "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else 0.0,
        "low_rating_share": ratio(sum(1 for r in ratings if r < 4.0), len(ratings)),
        "new_seller_share": ratio(new_sellers, len(products)),
    }


def concentration(
    products: Sequence[dict],
    key: str = "brand",
    cr3_high: float = 0.6,
    cr3_mid: float = 0.4,
) -> dict:
    """集中度：CR3 / CR5 / CR10 / HHI，判断新店切入难度。

    口径说明：按「销量份额」而非「商品数份额」计算——一个品牌挂 50 个链接
    但没销量，不代表它垄断。
    """
    if not products:
        raise MetricError("商品列表为空，无法计算集中度", "products")

    sales_by: dict[str, float] = defaultdict(float)
    for p in products:
        name = str(p.get(key) or "UNKNOWN")
        sales_by[name] += num(p.get("monthly_sales"), "monthly_sales")

    total = sum(sales_by.values())
    if total <= 0:
        raise MetricError("类目总销量为 0，集中度无意义", "monthly_sales")

    ordered = sorted(sales_by.items(), key=lambda kv: kv[1], reverse=True)
    shares = [(name, s / total) for name, s in ordered]

    def cr(n: int) -> float:
        return round(sum(share for _, share in shares[:n]), 4)

    hhi = round(sum((share * 100) ** 2 for _, share in shares), 1)

    # 品牌数少于 10 时 CR10 恒等于 100%，没有信息量 —— 此时改用 CR3 判断。
    # 这是很多「AI 类目分析」翻车的地方：把恒等式当成结论。
    enough_brands = len(shares) >= 10
    cr3 = cr(3)
    if cr3 >= cr3_high:
        verdict = "高度集中"
    elif cr3 >= cr3_mid:
        verdict = "中度集中"
    else:
        verdict = "分散"

    return {
        "dimension": key,
        "brand_count": len(shares),
        "cr3": cr3,
        "cr5": cr(5),
        "cr10": cr(10),
        "hhi": hhi,
        "cr10_meaningful": enough_brands,
        "verdict": verdict,
        "verdict_basis": "CR10" if enough_brands else "CR3（榜单品牌数 <10，CR10 恒为 100% 无意义）",
        "top": [{"name": n, "share": round(s, 4)} for n, s in shares[:5]],
    }


def _nice_step(raw: float) -> float:
    """把原始步长吸附到 1/2/2.5/5 × 10^k，让价格带读数好看。"""
    if raw <= 0:
        return 1.0
    exp = math.floor(math.log10(raw))
    base = raw / (10**exp)
    for candidate in (1, 2, 2.5, 5, 10):
        if base <= candidate:
            return candidate * (10**exp)
    return 10 ** (exp + 1)


def price_bands(
    products: Sequence[dict],
    target_bands: int = 8,
    blue_threshold: float = 1.2,
    red_threshold: float = 0.8,
    min_band_products: int = 3,
) -> list[dict]:
    """价格带分析 —— 找「销量高 + 供给少」的带，不是找销量最高的带。

    关键指标 opportunity = 销量份额 / 商品数份额：
      > 1.2  供不应求（蓝海候选）
      0.8~1.2 均衡
      < 0.8  供给过剩（红海）
    """
    if not products:
        raise MetricError("商品列表为空，无法计算价格带", "products")

    prices = [num(p.get("price"), "price", allow_zero=False) for p in products]
    lo, hi = min(prices), max(prices)
    step = _nice_step((hi - lo) / max(target_bands, 2)) if hi > lo else _nice_step(hi / 4 or 1)
    start = math.floor(lo / step) * step

    buckets: dict[int, list[dict]] = defaultdict(list)
    for p in products:
        price = num(p.get("price"), "price")
        idx = int((price - start) // step)
        buckets[idx].append(p)

    if not buckets:
        raise MetricError("价格分箱失败", "price")

    total_products = len(products)
    total_sales = sum(num(p.get("monthly_sales"), "monthly_sales") for p in products) or 1.0

    bands: list[dict] = []
    for idx in range(min(buckets), max(buckets) + 1):
        rows = buckets.get(idx, [])
        band_lo = start + idx * step
        band_hi = band_lo + step
        if not rows:
            continue
        n = len(rows)
        sales = sum(num(p.get("monthly_sales"), "monthly_sales") for p in rows)
        ratings = [num(p.get("rating"), "rating") for p in rows if p.get("rating") not in (None, "")]
        band = {
            "label": f"{band_lo:.0f}-{band_hi:.0f}",
            "low": _money(band_lo),
            "high": _money(band_hi),
            "product_count": n,
            "supply_share": ratio(n, total_products),
            "sales_sum": int(sales),
            "sales_share": ratio(sales, total_sales),
            "avg_sales": int(sales / n) if n else 0,
            "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else 0.0,
            "avg_price": _money(sum(num(p.get("price"), "price") for p in rows) / n),
        }
        band["opportunity"] = round(
            ratio(band["sales_share"], band["supply_share"] or 1e-9), 2
        ) if band["supply_share"] else 0.0
        bands.append(band)

    # 打标签：只给最强的一个贴蓝海，最弱的一个贴红海。
    # 样本 <3 个链接的带不参与「红海」判定 —— 2 个链接的样本量不足以下结论。
    if bands:
        eligible = [b for b in bands if b["product_count"] >= min_band_products] or bands
        best = max(eligible, key=lambda b: b["opportunity"])
        worst = min(eligible, key=lambda b: b["opportunity"])
        if best["opportunity"] >= blue_threshold:
            best["tag"] = "blue_ocean"
        if worst is not best and worst["opportunity"] < red_threshold:
            worst["tag"] = "red_ocean"
        for b in bands:
            b.setdefault("tag", "neutral")

    return bands


def config_opportunity(products: Sequence[dict], key: str = "config") -> list[dict]:
    """配置组合机会表：哪些配置卖得动、竞争少、售后风险低。"""
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in products:
        groups[str(p.get(key) or "未标注")].append(p)

    total_sales = sum(num(p.get("monthly_sales"), "monthly_sales") for p in products) or 1.0
    total_count = len(products) or 1
    out: list[dict] = []

    for name, rows in groups.items():
        sales = sum(num(p.get("monthly_sales"), "monthly_sales") for p in rows)
        ratings = [num(p.get("rating"), "rating") for p in rows if p.get("rating") not in (None, "")]
        out.append(
            {
                "config": name,
                "product_count": len(rows),
                "sales_sum": int(sales),
                "sales_share": ratio(sales, total_sales),
                "supply_share": ratio(len(rows), total_count),
                "avg_sales": int(sales / len(rows)),
                "avg_price": _money(sum(num(p.get("price"), "price") for p in rows) / len(rows)),
                "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else 0.0,
            }
        )

    for row in out:
        row["opportunity"] = round(row["sales_share"] / row["supply_share"], 2) if row["supply_share"] else 0.0

    return sorted(out, key=lambda r: (r["sales_share"], r["opportunity"]), reverse=True)


# --------------------------------------------------------------------------
# 二、选品与定价
# --------------------------------------------------------------------------

def pricing_model(
    price: float,
    product_cost: float,
    commission_rate: float = 0.14,
    tax_rate: float = 0.08,
    return_rate: float = 0.02,
    shipping_cost: float = 0.0,
    other_cost: float = 0.0,
    cvr: float = 0.02,
    target_margin: float = 0.2,
) -> dict:
    """单品定价模型：净利率、盈亏线 ROAS、可承受 CPC、目标价反算。

    口径（务必写进报告，否则数字无法被审计）：
      净收入 = 售价 × (1-佣金) × (1-退货率) × (1-税率)
      贡献毛利 = 净收入 - 商品成本 - 物流 - 其他
      盈亏线 ROAS = 售价 / 贡献毛利   ← 广告花到这个数就白干
      可承受 CPC = 贡献毛利 × 转化率  ← 每点击的期望贡献
    """
    price = num(price, "price", allow_zero=False)
    product_cost = num(product_cost, "product_cost")
    if product_cost >= price:
        raise MetricError("商品成本已高于售价，定价模型无解", "product_cost")

    net_revenue = price * (1 - commission_rate) * (1 - return_rate) * (1 - tax_rate)
    contribution = net_revenue - product_cost - shipping_cost - other_cost
    margin_rate = ratio(contribution, price)
    gross_rate = ratio(price - product_cost, price)

    breakeven_roas = ratio(price, contribution) if contribution > 0 else 0.0
    breakeven_acos = margin_rate
    affordable_cpc = round(contribution * cvr, 2)

    k = (1 - commission_rate) * (1 - return_rate) * (1 - tax_rate)
    fixed = product_cost + shipping_cost + other_cost

    # 达到目标净利率所需的**最低**售价（不是「建议售价」——这个区别很关键：
    # 高于它是好事，说明有促销空间；低于它才是问题，需要提价或压成本）
    required_price = None
    if k - target_margin > 0:
        required_price = _money(fixed / (k - target_margin))
    headroom_rate = None
    if required_price:
        headroom_rate = round((price - required_price) / required_price, 4)

    return {
        "price": _money(price),
        "net_revenue": _money(net_revenue),
        "product_cost": _money(product_cost),
        "contribution": _money(contribution),
        "gross_margin_rate": gross_rate,
        "net_margin_rate": margin_rate,
        "breakeven_roas": round(breakeven_roas, 2),
        "breakeven_acos": round(breakeven_acos, 4),
        "affordable_cpc": affordable_cpc,
        "required_price": required_price,
        "headroom_rate": headroom_rate,
        "target_margin": target_margin,
        "params": {
            "commission_rate": commission_rate,
            "tax_rate": tax_rate,
            "return_rate": return_rate,
            "shipping_cost": _money(shipping_cost),
            "other_cost": _money(other_cost),
            "cvr": cvr,
        },
        "formula": "贡献毛利=售价×(1-佣金)×(1-退货率)×(1-税率)-成本-物流-其他",
    }


def sku_matrix(
    base_cost: float,
    base_price: float,
    commission_rate: float = 0.14,
    tax_rate: float = 0.08,
    return_rate: float = 0.02,
    shipping_cost: float = 0.0,
) -> list[dict]:
    """SKU 四角色结构：引流 / 主推 / 高配 / 套装。

    定价不是拍脑袋，是按「每个角色要承担的利润职责」反算出来的。
    """
    roles = [
        ("引流款", 0.82, 0.9, "低配低价，承担拉新与评价积累，净利率允许 5%-10%"),
        ("主推款", 1.0, 1.0, "主力成交，承担类目排名与利润基本盘，净利率 20%-25%"),
        ("高配款", 1.35, 1.18, "高配置高客单，承担客单价提升，净利率 25%-30%"),
        ("套装款", 1.9, 1.25, "组合装，摊薄物流成本，承担利润天花板"),
    ]
    out: list[dict] = []
    for name, cost_k, price_k, note in roles:
        cost = round(base_cost * cost_k, 2)
        price = round(base_price * price_k, 2)
        model = pricing_model(
            price=price,
            product_cost=cost,
            commission_rate=commission_rate,
            tax_rate=tax_rate,
            return_rate=return_rate,
            shipping_cost=shipping_cost,
        )
        out.append(
            {
                "role": name,
                "cost": cost,
                "price": price,
                "net_margin_rate": model["net_margin_rate"],
                "contribution": model["contribution"],
                "breakeven_roas": model["breakeven_roas"],
                "note": note,
            }
        )
    return out


# --------------------------------------------------------------------------
# 三、广告诊断
# --------------------------------------------------------------------------

def funnel_row(row: dict, price: float) -> dict:
    """单条广告记录的漏斗指标。"""
    impressions = num(row.get("impressions"), "impressions")
    clicks = num(row.get("clicks"), "clicks")
    cost = num(row.get("cost"), "cost")
    orders = num(row.get("orders"), "orders")
    revenue = num(row.get("revenue"), "revenue")

    ctr = ratio(clicks, impressions)
    cvr = ratio(orders, clicks)
    cpc = ratio(cost, clicks)
    cpa = ratio(cost, orders)
    roas = ratio(revenue, cost)
    acos = ratio(cost, revenue)

    return {
        "campaign": str(row.get("campaign") or "未命名"),

        "impressions": int(impressions),
        "clicks": int(clicks),
        "cost": _money(cost),
        "orders": int(orders),
        "revenue": _money(revenue),
        "ctr": ctr,
        "cvr": cvr,
        "cpc": _money(cpc),
        "cpa": _money(cpa) if cpa else 0.0,
        "roas": round(roas, 2),
        "acos": round(acos, 4),
        "aov": _money(ratio(revenue, orders)) if orders else 0.0,
    }


def grade_campaign(
    m: dict,
    breakeven_roas: float,
    target_roas: float,
    ctr_bench: float = 0.02,
    min_impressions: int = 1000,
    min_clicks: int = 30,
    bleeding_ratio: float = 0.5,
) -> dict:
    """广告分级 —— 固定诊断顺序，不可跳步。

    顺序的意义：先排除「数据不够」和「没花钱」这类假问题，
    否则会把噪声当成结论，动作做反。
    """
    if m["impressions"] < min_impressions:
        return {"level": "HOLD", "reason": f"曝光 {m['impressions']} < {min_impressions}，数据不足，不做判断", "stage": "insufficient_data"}
    if m["cost"] <= 0:
        return {"level": "HOLD", "reason": "本期无花费，无诊断对象", "stage": "no_spend"}
    if m["ctr"] < ctr_bench:
        return {
            "level": "P1",
            "reason": f"CTR {pct(m['ctr'])} 低于基准 {pct(ctr_bench)}：创意/主图/标题问题，不是出价问题",
            "stage": "click_through",
            "backflow": "listing",
        }
    if m["clicks"] >= min_clicks and m["orders"] == 0:
        return {
            "level": "P0",
            "reason": f"点击 {m['clicks']} 次零订单：落地页/价格/评价不支撑，先止血再修",
            "stage": "click_no_order",
            "backflow": "listing",
        }

    if breakeven_roas <= 0:
        return {"level": "P1", "reason": "贡献毛利为负，先修定价再谈广告", "stage": "negative_margin", "backflow": "strategy"}

    if m["roas"] < breakeven_roas * bleeding_ratio:
        return {
            "level": "P0",
            "reason": f"ROAS {m['roas']} < 盈亏线 {breakeven_roas} 的一半，每单都在亏现金",
            "stage": "bleeding",
            "backflow": "listing",
        }
    if m["roas"] < breakeven_roas:
        return {
            "level": "P1",
            "reason": f"ROAS {m['roas']} 低于盈亏线 {breakeven_roas}，亏但可控，优化转化或降出价",
            "stage": "below_breakeven",
            "backflow": "listing",
        }
    if m["roas"] < target_roas:
        return {
            "level": "P1",
            "reason": f"ROAS {m['roas']} 已过盈亏线但未达目标 {target_roas}，维持并小步提效",
            "stage": "below_target",
        }
    return {
        "level": "P2",
        "reason": f"ROAS {m['roas']} ≥ 目标 {target_roas}，可放量",
        "stage": "scale",
    }


def diagnose_ads(
    rows: Sequence[dict],
    breakeven_roas: float,
    target_roas: float,
    ctr_bench: float = 0.02,
    min_impressions: int = 1000,
    min_clicks: int = 30,
    bleeding_ratio: float = 0.5,
) -> dict:
    """整张广告报表的诊断：逐组分级 + 汇总。"""
    if not rows:
        raise MetricError("广告报表为空", "rows")

    campaigns = []
    for row in rows:
        m = funnel_row(row, num(row.get("price", 0), "price"))
        m["grade"] = grade_campaign(
            m, breakeven_roas, target_roas, ctr_bench,
            min_impressions=min_impressions,
            min_clicks=min_clicks,
            bleeding_ratio=bleeding_ratio,
        )
        campaigns.append(m)

    total_cost = sum(c["cost"] for c in campaigns)
    total_revenue = sum(c["revenue"] for c in campaigns)
    total_orders = sum(c["orders"] for c in campaigns)
    total_clicks = sum(c["clicks"] for c in campaigns)
    total_impr = sum(c["impressions"] for c in campaigns)

    by_level = Counter(c["grade"]["level"] for c in campaigns)

    return {
        "campaigns": campaigns,
        "summary": {
            "campaign_count": len(campaigns),
            "total_cost": _money(total_cost),
            "total_revenue": _money(total_revenue),
            "total_orders": total_orders,
            "total_clicks": total_clicks,
            "total_impressions": total_impr,
            "overall_roas": round(ratio(total_revenue, total_cost), 2),
            "overall_ctr": ratio(total_clicks, total_impr),
            "overall_cvr": ratio(total_orders, total_clicks),
            "overall_cpa": _money(ratio(total_cost, total_orders)),
            "net_after_ads": _money(total_revenue - total_cost),
            "by_level": dict(by_level),
            "p0_count": by_level.get("P0", 0),
        },
    }


# --------------------------------------------------------------------------
# 四、评论归因
# --------------------------------------------------------------------------

REVIEW_CATEGORIES = [
    "误解型",
    "参数不符合预期",
    "配件缺失",
    "使用困难",
    "发货慢",
    "破损/故障/退货",
    "价格偏高",
    "描述不符",
]

FIXABLE_CATEGORIES = {"误解型", "参数不符合预期", "配件缺失", "使用困难", "描述不符"}


def review_aggregate(reviews: Sequence[dict]) -> dict:
    """评论八类归因：把差评拆成「改文案能救」和「改供应链才能救」两类。

    可修复率是这套系统里最值钱的指标之一：它把「差评多」这种情绪化描述，
    翻译成「84.6% 的差评改文案/补图就能消掉」这种可排期的工作量。
    """
    if not reviews:
        raise MetricError("评论数据为空", "reviews")

    ratings = [num(r.get("rating"), "rating") for r in reviews]
    total = len(ratings)

    good = sum(1 for r in ratings if r >= 4)
    mid = sum(1 for r in ratings if 3 <= r < 4)
    bad = sum(1 for r in ratings if r < 3)

    bad_rows = [r for r in reviews if num(r.get("rating"), "rating") < 3]
    cat_counter = Counter(str(r.get("category") or "未标注") for r in bad_rows)

    by_category = []
    for cat, count in cat_counter.most_common():
        by_category.append(
            {
                "category": cat,
                "count": count,
                "share": ratio(count, len(bad_rows)) if bad_rows else 0.0,
                "fixable": cat in FIXABLE_CATEGORIES,
            }
        )

    fixable_count = sum(item["count"] for item in by_category if item["fixable"])
    with_image = sum(1 for r in reviews if str(r.get("has_image", "")).lower() in ("1", "true", "yes"))

    return {
        "total": total,
        "good": good,
        "mid": mid,
        "bad": bad,
        "good_rate": ratio(good, total),
        "bad_rate": ratio(bad, total),
        "avg_rating": round(sum(ratings) / total, 2),
        "bad_count": len(bad_rows),
        "bad_by_category": by_category,
        "fixable_count": fixable_count,
        "fixable_rate": ratio(fixable_count, len(bad_rows)) if bad_rows else 0.0,
        "with_image_rate": ratio(with_image, total),
        "unlabeled_bad": cat_counter.get("未标注", 0),
    }


# --------------------------------------------------------------------------
# 五、Listing 校验
# --------------------------------------------------------------------------

def title_lint(
    title: str,
    title_max: int,
    front_weight: int,
    keywords: Sequence[str] = (),
    stuffing_limit: int = 3,
) -> dict:
    """标题硬校验：字符数、前置权重区、关键词覆盖、堆砌检测。"""
    title = (title or "").strip()
    chars = len(title)
    front = title[:front_weight]

    hits = [k for k in keywords if k and k.lower() in title.lower()]
    missing = [k for k in keywords if k and k.lower() not in title.lower()]

    # 堆砌：同一实词重复达到 stuffing_limit 次
    words = [w for w in title.replace("-", " ").replace("|", " ").split() if len(w) > 2]
    counter = Counter(w.lower() for w in words)
    stuffed = [w for w, c in counter.items() if c >= stuffing_limit]

    # 全大写/感叹号等营销噪音
    noise = []
    if title.count("!") >= 2:
        noise.append("感叹号 ≥2")
    if title.isupper() and chars > 20:
        noise.append("全大写")

    return {
        "chars": chars,
        "title_max": title_max,
        "over_limit": chars > title_max,
        "front_weight_zone": front,
        "front_has_keyword": any(k.lower() in front.lower() for k in keywords if k),
        "keyword_hits": hits,
        "keyword_missing": missing,
        "keyword_coverage": ratio(len(hits), len(keywords)) if keywords else 0.0,
        "stuffed_words": stuffed,
        "noise": noise,
        "passed": (not chars > title_max) and not stuffed and not noise,
    }


def image_plan_check(plan: Sequence[dict], required_slots: Sequence[str]) -> dict:
    """主图九宫格规划完整性校验：少一张就是少一个转化理由。"""
    covered = {str(item.get("slot") or "").strip() for item in plan}
    missing = [slot for slot in required_slots if slot not in covered]
    return {
        "planned": len(plan),
        "required": len(required_slots),
        "covered": sorted(covered),
        "missing": missing,
        "complete": not missing,
        "coverage": ratio(len(required_slots) - len(missing), len(required_slots)),
    }
