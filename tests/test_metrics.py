"""核心计算层单测 —— 这些数字如果错了，整套系统的结论都是错的。

跑法：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import metrics as M


class TestPricing(unittest.TestCase):
    def test_breakeven_roas_is_inverse_of_margin(self):
        """盈亏线 ROAS 必须等于 1/净利率，否则广告止损线就是错的。"""
        m = M.pricing_model(price=350, product_cost=168, commission_rate=0.14, tax_rate=0.08, return_rate=0.02, shipping_cost=22)
        self.assertAlmostEqual(m["breakeven_roas"], round(1 / m["net_margin_rate"], 2), places=2)

    def test_cost_above_price_raises(self):
        with self.assertRaises(M.MetricError):
            M.pricing_model(price=100, product_cost=120)

    def test_required_price_hits_target_margin(self):
        """用 required_price 反算回去，净利率必须正好等于目标值。"""
        target = 0.2
        m = M.pricing_model(price=300, product_cost=168, commission_rate=0.14, tax_rate=0.08, return_rate=0.02, shipping_cost=22, target_margin=target)
        check = M.pricing_model(price=m["required_price"], product_cost=168, commission_rate=0.14, tax_rate=0.08, return_rate=0.02, shipping_cost=22, target_margin=target)
        self.assertAlmostEqual(check["net_margin_rate"], target, places=2)

    def test_affordable_cpc_scales_with_cvr(self):
        low = M.pricing_model(price=350, product_cost=168, cvr=0.01)
        high = M.pricing_model(price=350, product_cost=168, cvr=0.02)
        self.assertAlmostEqual(high["affordable_cpc"], low["affordable_cpc"] * 2, places=1)


class TestConcentration(unittest.TestCase):
    def test_few_brands_uses_cr3(self):
        """品牌数 <10 时 CR10 恒为 100%，必须降级用 CR3 判断。"""
        products = [
            {"brand": "A", "monthly_sales": 100},
            {"brand": "B", "monthly_sales": 50},
            {"brand": "C", "monthly_sales": 30},
        ]
        c = M.concentration(products)
        self.assertFalse(c["cr10_meaningful"])
        self.assertEqual(c["cr10"], 1.0)
        self.assertAlmostEqual(c["cr3"], 1.0)
        self.assertIn("CR3", c["verdict_basis"])


class TestPriceBands(unittest.TestCase):
    def test_opportunity_flags_blue_ocean(self):
        products = []
        # 低价带：商品多、销量低（红海）
        for i in range(10):
            products.append({"price": 200, "monthly_sales": 20, "brand": f"B{i}", "rating": 4.2})
        # 中价带：商品少、销量高（蓝海）
        for i in range(3):
            products.append({"price": 330, "monthly_sales": 900, "brand": f"C{i}", "rating": 4.4})
        bands = M.price_bands(products)
        blue = [b for b in bands if b["tag"] == "blue_ocean"]
        self.assertEqual(len(blue), 1)
        self.assertGreater(blue[0]["opportunity"], 1.2)

    def test_small_sample_not_labelled_red(self):
        """只有 1 个链接的带不能判为红海 —— 样本量不足。"""
        products = [{"price": 100, "monthly_sales": 500, "brand": "A", "rating": 4.5} for _ in range(10)]
        products.append({"price": 900, "monthly_sales": 1, "brand": "Z", "rating": 4.0})
        bands = M.price_bands(products)
        red = [b for b in bands if b["tag"] == "red_ocean"]
        self.assertEqual(red, [])


class TestAds(unittest.TestCase):
    def _row(self, **kw):
        base = {"campaign": "t", "impressions": 10000, "clicks": 300, "cost": 100, "orders": 10, "revenue": 3500}
        base.update(kw)
        return base

    def test_insufficient_data_first(self):
        """诊断顺序第一条：曝光不足直接 HOLD，不许往后续规则走。"""
        m = M.funnel_row(self._row(impressions=500), price=350)
        g = M.grade_campaign(m, breakeven_roas=3.4, target_roas=7)
        self.assertEqual(g["level"], "HOLD")
        self.assertEqual(g["stage"], "insufficient_data")

    def test_zero_order_outranks_roas(self):
        # CTR 必须先过基准，否则会先被判定为「创意问题」而不是「零单问题」
        m = M.funnel_row(self._row(orders=0, revenue=0, clicks=300), price=350)
        g = M.grade_campaign(m, breakeven_roas=3.4, target_roas=7)
        self.assertEqual(g["level"], "P0")
        self.assertEqual(g["stage"], "click_no_order")

    def test_bleeding_is_p0(self):
        m = M.funnel_row(self._row(cost=500, orders=1, revenue=350, clicks=400), price=350)
        g = M.grade_campaign(m, breakeven_roas=3.4, target_roas=7)
        self.assertEqual(g["level"], "P0")

    def test_scale_is_p2(self):
        m = M.funnel_row(self._row(cost=100, orders=12, revenue=4200), price=350)
        g = M.grade_campaign(m, breakeven_roas=3.4, target_roas=7)
        self.assertEqual(g["level"], "P2")


class TestReview(unittest.TestCase):
    def test_fixable_rate(self):
        reviews = [
            {"rating": 1, "category": "误解型"},
            {"rating": 2, "category": "配件缺失"},
            {"rating": 1, "category": "破损/故障/退货"},
            {"rating": 5, "category": "未标注"},
        ]
        a = M.review_aggregate(reviews)
        self.assertEqual(a["bad_count"], 3)
        self.assertEqual(a["fixable_count"], 2)
        self.assertAlmostEqual(a["fixable_rate"], 0.6667, places=3)


class TestTitleLint(unittest.TestCase):
    def test_stuffing_detected(self):
        r = M.title_lint("patinete patinete patinete eletrico", title_max=120, front_weight=30, keywords=["patinete eletrico"])
        self.assertFalse(r["passed"])
        self.assertIn("patinete", r["stuffed_words"])

    def test_over_limit_detected(self):
        r = M.title_lint("x" * 130, title_max=60, front_weight=60, keywords=[])
        self.assertTrue(r["over_limit"])
        self.assertFalse(r["passed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
