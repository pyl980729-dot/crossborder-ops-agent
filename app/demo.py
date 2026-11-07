"""演示数据装载 —— 把种子 CSV 组装成一次完整运行的输入。

单独抽出来是为了让 CLI、HTTP 接口、测试共用同一份数据，
避免出现「命令行能跑但接口跑不了」这种低级不一致。
"""

from __future__ import annotations

import csv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SEED_DIR = BASE_DIR / "data" / "seed"


def _read_csv(name: str, fallback: str | None = None) -> list[dict]:
    """读种子 CSV。name 不存在时退到 fallback。

    存在「日报」时优先用日报 —— 真实导出的广告/评论表几乎都带日期，
    日期是趋势对比的前提，演示数据也应该反映这一点。
    """
    path = SEED_DIR / name
    if not path.exists():
        if fallback and (SEED_DIR / fallback).exists():
            path = SEED_DIR / fallback
        else:
            raise FileNotFoundError(f"种子数据缺失: {path}，请先运行 python scripts/gen_seed.py")
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def build_demo_payload() -> dict:
    products = _read_csv("category_scooter.csv")
    for row in products:
        row["price"] = float(row["price"])
        row["monthly_sales"] = int(row["monthly_sales"])
        row["rating"] = float(row["rating"])

    campaigns = _read_csv("ads_daily.csv", fallback="ads.csv")
    for row in campaigns:
        row["impressions"] = int(row["impressions"])
        row["clicks"] = int(row["clicks"])
        row["cost"] = float(row["cost"])
        row["orders"] = int(row["orders"])
        row["revenue"] = float(row["revenue"])

    reviews = _read_csv("reviews_daily.csv", fallback="reviews.csv")
    for row in reviews:
        row["rating"] = float(row["rating"])
        row["has_image"] = int(row["has_image"])

    return {
        "category": "Patinete Elétrico · Shopee BR",
        "platform": "shopee_br",
        "products": products,
        "product_cost": 168.0,
        "target_price": 350.0,
        "shipping_cost": 22.0,
        "product_name": "Patinete Elétrico Adulto 500W 10Ah",
        "selling_points": [
            "30km 续航，一周只充两次电",
            "3 秒折叠，能塞进后备箱",
            "双刹 + 防滑胎，雨天不打滑",
        ],
        "keywords": [
            {"term": "patinete elétrico", "volume": 74000},
            {"term": "scooter elétrico adulto", "volume": 41000},
            {"term": "patinete 500w", "volume": 18500},
            {"term": "patinete dobrável", "volume": 12800},
            {"term": "patinete elétrico 30km", "volume": 6400},
        ],
        "specs": {"capacity": "10Ah", "power": "500W", "size": "108×42×48cm"},
        "target_audience": "通勤 5-10km 的城市上班族",
        "campaigns": campaigns,
        "reviews": reviews,
    }
