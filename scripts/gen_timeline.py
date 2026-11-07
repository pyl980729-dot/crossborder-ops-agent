"""生成带日期的种子数据 —— 让趋势对比有东西可看。

为什么单独一个脚本而不是改 gen_seed.py：
那份生成的是「一张汇总表」，这份生成的是「30 天日报」，
两者演示的是不同场景（有日期列 / 没日期列），都需要保留。

数据里刻意埋了四个业务故事，用来验证系统能不能如实说出来：

1. BR-Patinete-低价引流：一直失血，最近改了素材+降了预算，ROAS 从 0.7 回到 2.7
2. BR-Scooter-竞品词：30 天零成交，花费还在涨 —— 典型的「该关没关」
3. BR-Acessórios-配件：一直健康，最近两周 ROAS 掉到盈亏线以下 —— 整体向好时被平均掩盖
4. 评论：整体差评率在降，但「配件缺失」最近 10 天集中出现 —— 包装环节出了问题

第 3、4 条是重点：**只看整体会漏掉它们**，这正是逐组对比和分类趋势存在的理由。
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "data" / "seed"

END = date(2026, 11, 7)
DAYS = 30
START = END - timedelta(days=DAYS - 1)

rng = random.Random(20261107)

# 名称, 日均曝光, 日均点击, 日均花费, 日均销售额(基线), 客单价
#   基线数字取自 ads.csv（那张表被当作「一天的量」，30 天就是 30 倍）
CAMPAIGNS = [
    ("BR-Scooter-500W-广泛", 48200, 1310, 420.50, 4200.0, 350, (1.00, 1.06), (1.00, 1.02)),
    ("BR-Scooter-500W-精准", 18600, 742, 268.30, 2800.0, 350, (1.00, 1.02), (1.00, 1.00)),
    ("BR-Patinete-低价引流", 32400, 1180, 512.80, 350.0, 350, (1.00, 3.20), (1.00, 0.80)),
    ("BR-Scooter-竞品词", 9200, 386, 186.40, 0.0, 350, (1.00, 0.0), (1.00, 1.25)),
    ("BR-Acessórios-配件", 12400, 108, 38.20, 240.0, 350, (1.00, 0.58), (1.00, 1.02)),
    ("BR-Scooter-800W-放量", 21600, 1296, 302.10, 2645.0, 350, (1.00, 1.40), (1.00, 1.30)),
]

# 最近 14 天才开始变化：这样「近 7 天 vs 前 7 天」才有差异，
# 而更早的 16 天保持基线，避免整段数据都被污染
RAMP_FROM = DAYS - 14


def _ramp(day: int, lo: float, hi: float) -> float:
    """最近 14 天线性过渡；之前保持 lo。"""
    if day < RAMP_FROM:
        return lo
    t = (day - RAMP_FROM) / (DAYS - 1 - RAMP_FROM)
    return lo + (hi - lo) * t


def gen_ads_daily() -> list[dict]:
    rows: list[dict] = []
    for name, impr, clicks, cost, revenue, aov, rev_shape, cost_shape in CAMPAIGNS:
        for day in range(DAYS):
            d = START + timedelta(days=day)
            noise = rng.uniform(0.88, 1.12)
            rev_f = _ramp(day, *rev_shape)
            cost_f = _ramp(day, *cost_shape)

            day_cost = cost * cost_f * noise
            day_rev = revenue * rev_f * noise
            orders = int(round(day_rev / aov)) if day_rev else 0
            # 有点击没订单是常态，别让 orders=0 把 clicks 也归零
            day_clicks = max(1, int(round(clicks * noise * (0.95 + 0.1 * (1 if day % 3 == 0 else 0)))))
            day_impr = max(day_clicks, int(round(impr * noise)))

            rows.append(
                {
                    "date": d.isoformat(),
                    "campaign": name,
                    "impressions": day_impr,
                    "clicks": day_clicks,
                    "cost": round(day_cost, 2),
                    "orders": orders,
                    "revenue": round(day_rev, 2),
                }
            )
    return rows


REVIEW_DAYS = 60  # 评论比广告稀疏，铺更长一段时间，才够按月对比


def gen_reviews_daily() -> list[dict]:
    """给现有评论打上日期。

    分布规则：
    - 好评均匀铺满 60 天
    - 配件缺失的差评集中在最近 14 天（模拟包装环节最近才出问题）
    - 其余差评均匀铺开

    为什么不铺 30 天：21 条差评 / 30 天 = 每周 5 条，
    按月对比时两边各 10 条左右，刚够判断；按周对比根本不够，
    所以评论的窗口默认是 28 天而不是 7 天。
    """
    with (BASE / "reviews.csv").open(encoding="utf-8") as fh:
        reviews = list(csv.DictReader(fh))

    good = [r for r in reviews if float(r["rating"]) >= 4]
    bad_parts = [r for r in reviews if float(r["rating"]) < 3 and r["category"] == "配件缺失"]
    bad_rest = [r for r in reviews if float(r["rating"]) < 3 and r["category"] != "配件缺失"]

    out: list[dict] = []

    review_start = END - timedelta(days=REVIEW_DAYS - 1)

    def assign(group: list[dict], day_lo: int, day_hi: int) -> None:
        """均匀铺开。

        不能用 i % span —— 当条数少于天数时，所有数据会挤在前几天，
        看起来就像「最近没有评论」。第一版就是这么错的。
        """
        n = len(group)
        span = day_hi - day_lo
        for i, r in enumerate(group):
            offset = round(i * span / (n - 1)) if n > 1 else 0
            out.append({**r, "date": (review_start + timedelta(days=day_lo + offset)).isoformat()})

    assign(good, 0, REVIEW_DAYS - 1)
    assign(bad_rest, 0, REVIEW_DAYS - 1)
    assign(bad_parts, REVIEW_DAYS - 14, REVIEW_DAYS - 1)

    # 打乱顺序：真实导出不会按日期排序，也顺便验证系统不依赖行序
    rng.shuffle(out)
    return out


def write(name: str, rows: list[dict]) -> None:
    path = BASE / name
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    dates = sorted(r["date"] for r in rows if r.get("date"))
    span = f"{dates[0]} ~ {dates[-1]}" if dates else "无日期"
    print(f"  {name:<20}{len(rows):>4} 行  {span}")


if __name__ == "__main__":
    print(f"生成 30 天日报：{START} ~ {END}")
    write("ads_daily.csv", gen_ads_daily())
    write("reviews_daily.csv", gen_reviews_daily())
