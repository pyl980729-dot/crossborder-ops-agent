"""生成演示用种子数据 —— 确定性随机，每次跑出同一份数据。

数据设计刻意埋了三个可被诊断出来的问题，用来验证 Agent 真的在算而不是在编：
1. 280-400 价格带供给少但销量集中（蓝海机会）
2. 一个头部品牌吃掉近 40% 销量（CR10 偏高）
3. 广告里有一个组 ROAS 0.6（P0 失血），一个组零订单（落地页问题）
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "data" / "seed"
random.seed(20261106)

BRANDS = [
    ("VoltRide", 0.34), ("MoviPro", 0.18), ("ZetaWheel", 0.12),
    ("UrbanGlide", 0.09), ("SpeedOne", 0.07), ("CasaMove", 0.06),
    ("Generic", 0.14),
]
CONFIGS = ["350W/7.5Ah", "500W/10Ah", "500W/15Ah", "800W/15Ah", "800W/20Ah"]
SCENE_WORDS = ["Patinete Elétrico", "Scooter Elétrico Adulto", "Patinete 500W", "Scooter Dobrável"]


def weighted_brand() -> str:
    r = random.random()
    acc = 0.0
    for name, w in BRANDS:
        acc += w
        if r <= acc:
            return name
    return "Generic"


def gen_products(n: int = 42) -> list[dict]:
    rows = []
    for i in range(n):
        # 价格分三层：低价红海、中价蓝海、高价小众
        tier = random.choices(["low", "mid", "high"], weights=[0.42, 0.38, 0.20])[0]
        if tier == "low":
            price = round(random.uniform(180, 280), 2)
            sales = random.randint(80, 900)
        elif tier == "mid":
            price = round(random.uniform(285, 400), 2)
            sales = random.randint(300, 2600)   # 供给少但销量高 → 蓝海
        else:
            price = round(random.uniform(410, 900), 2)
            sales = random.randint(40, 700)

        brand = weighted_brand()
        if brand == "VoltRide":
            sales = int(sales * 2.1)            # 头部吃掉更多量

        rating = round(random.uniform(3.6, 4.9) if tier != "low" else random.uniform(3.3, 4.5), 1)
        rows.append(
            {
                "sku": f"SC-{1000 + i}",
                "title": f"{random.choice(SCENE_WORDS)} {random.choice(CONFIGS)} {brand}",
                "brand": brand,
                "seller": f"seller_{random.randint(1, 28):02d}",
                "price": price,
                "monthly_sales": sales,
                "rating": rating,
                "review_count": int(sales * random.uniform(0.1, 0.6)),
                "config": random.choice(CONFIGS),
                "is_new_seller": random.choice([0, 0, 0, 1]),
            }
        )
    return rows


def gen_ads() -> list[dict]:
    """6 个广告组，刻意覆盖 P0/P1/P2 全部诊断路径。

    假设客单价 350、贡献毛利约 101 → 盈亏线 ROAS≈3.45、目标 ROAS≈7.16。
    """
    return [
        # 组名, 曝光, 点击, 花费, 订单, 收入
        {"campaign": "BR-Scooter-500W-广泛", "impressions": 48200, "clicks": 1310, "cost": 420.50, "orders": 12, "revenue": 4200.00},   # ROAS 9.99 → P2
        {"campaign": "BR-Scooter-500W-精准", "impressions": 18600, "clicks": 742, "cost": 268.30, "orders": 8, "revenue": 2800.00},    # ROAS 10.44 → P2
        {"campaign": "BR-Patinete-低价引流", "impressions": 32400, "clicks": 1180, "cost": 512.80, "orders": 1, "revenue": 350.00},    # ROAS 0.68 → P0 失血
        {"campaign": "BR-Scooter-竞品词", "impressions": 9200, "clicks": 386, "cost": 186.40, "orders": 0, "revenue": 0.00},           # 386 点击零单 → P0
        {"campaign": "BR-Acessórios-配件", "impressions": 12400, "clicks": 108, "cost": 38.20, "orders": 6, "revenue": 240.00},        # CTR 0.87% → P1
        {"campaign": "BR-Scooter-800W-放量", "impressions": 21600, "clicks": 1296, "cost": 302.10, "orders": 3, "revenue": 1050.00},   # ROAS 3.48 → P1 临界
    ]


REVIEW_TEMPLATES = [
    ("误解型", 1, "Achei que viesse com a bateria extra, mas não vem. Deveria estar mais claro.", 0),
    ("误解型", 2, "O produto é menor do que parece nas fotos.", 1),
    ("误解型", 2, "Não sabia que precisava montar. Perdi tempo.", 0),
    ("参数不符合预期", 2, "A autonomia real é bem menor que os 30km anunciados. Fez uns 18km.", 1),
    ("参数不符合预期", 1, "A potência não parece 500W, sobe ladeira com dificuldade.", 0),
    ("配件缺失", 2, "Faltou o carregador na caixa. Tive que comprar outro.", 1),
    ("配件缺失", 1, "Veio sem o manual e sem a chave de montagem.", 0),
    ("使用困难", 2, "Difícil de dobrar, precisa de muita força.", 0),
    ("使用困难", 1, "Não consegui configurar o app de jeito nenhum.", 1),
    ("发货慢", 1, "Demorou 28 dias para chegar, muito além do prazo.", 0),
    ("发货慢", 2, "Entrega atrasou e ninguém respondeu minhas mensagens.", 0),
    ("破损/故障/退货", 1, "Chegou com a roda traseira amassada.", 1),
    ("破损/故障/退货", 1, "Parou de funcionar na segunda semana.", 0),
    ("价格偏高", 2, "Bom produto mas caro pelo que oferece.", 0),
    ("描述不符", 2, "A cor é diferente da foto, veio mais escuro.", 1),
]

GOOD_TEMPLATES = [
    ("", 5, "Excelente custo-benefício, chegou rápido e funciona muito bem.", 1),
    ("", 5, "Muito bom para ir ao trabalho, dobra fácil e cabe no porta-malas.", 1),
    ("", 4, "Boa construção, só achei um pouco pesado.", 0),
    ("", 5, "A bateria surpreendeu, uso 3 dias sem carregar.", 1),
    ("", 4, "Atendeu as expectativas, entrega dentro do prazo.", 0),
]


def gen_reviews(total: int = 450) -> list[dict]:
    """评论种子。

    数量给到 450 而不是 150：评论比广告稀疏得多，按月做趋势对比时，
    150 条（差评 21 条）摊到两个月里，每边只有 10 条 —— 刚够看，稍少一点就全是噪声。
    450 条能让「按月对比」这个口径站得住。
    """
    rows = []
    bad_target = int(total * 0.145)
    for i in range(bad_target):
        cat, rating, text, img = random.choice(REVIEW_TEMPLATES)
        # 误解型刻意做多，用来演示"可修复率高"
        if i % 3 == 0:
            cat, rating, text, img = REVIEW_TEMPLATES[0]
        # 配件缺失保持 ~8%：太低演示不出"某类问题突然变多"的场景
        if i % 12 == 5:
            cat, rating, text, img = random.choice(
                [t for t in REVIEW_TEMPLATES if t[0] == "配件缺失"]
            )
        rows.append({"sku": "SC-1000", "rating": rating, "text": text, "category": cat, "has_image": img})
    while len(rows) < total:
        cat, rating, text, img = random.choice(GOOD_TEMPLATES)
        rows.append({"sku": "SC-1000", "rating": rating, "text": text, "category": cat or "未标注", "has_image": img})
    random.shuffle(rows)
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {path.name}: {len(rows)} rows")


if __name__ == "__main__":
    write_csv(BASE / "category_scooter.csv", gen_products())
    write_csv(BASE / "ads.csv", gen_ads())
    write_csv(BASE / "reviews.csv", gen_reviews())
