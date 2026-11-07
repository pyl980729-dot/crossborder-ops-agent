"""阈值配置测试 —— 重点验证「同一份数据，阈值不同结论就不同」这件事真的成立。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.core import config
from app.core import metrics as M

db.init_db()


class TestPriority(unittest.TestCase):
    def tearDown(self):
        config.reset_all()

    def test_platform_overrides_default(self):
        """Amazon 的 CTR 基准必须和 Shopee 不一样，否则平台预设形同虚设。"""
        sh = config.load_config("shopee_br")
        az = config.load_config("amazon")
        self.assertNotEqual(sh.get("ads.ctr_bench"), az.get("ads.ctr_bench"))
        self.assertEqual(az.source_of("ads.ctr_bench"), "platform")

    def test_unknown_platform_falls_back(self):
        c = config.load_config("not_a_platform")
        self.assertEqual(c.source_of("ads.ctr_bench"), "default")

    def test_request_overrides_platform(self):
        c = config.load_config("amazon", {"ads.ctr_bench": 0.05})
        self.assertAlmostEqual(c.get("ads.ctr_bench"), 0.05)
        self.assertEqual(c.source_of("ads.ctr_bench"), "request")

    def test_saved_override_beats_platform(self):
        db.save_settings({"ads.ctr_bench": 0.033})
        c = config.load_config("amazon")
        self.assertAlmostEqual(c.get("ads.ctr_bench"), 0.033)
        self.assertEqual(c.source_of("ads.ctr_bench"), "override")

    def test_request_beats_saved_override(self):
        db.save_settings({"ads.ctr_bench": 0.033})
        c = config.load_config("amazon", {"ads.ctr_bench": 0.08})
        self.assertAlmostEqual(c.get("ads.ctr_bench"), 0.08)
        self.assertEqual(c.source_of("ads.ctr_bench"), "request")


class TestValidation(unittest.TestCase):
    def tearDown(self):
        config.reset_all()

    def test_out_of_range_rejected(self):
        """0.99 的 CTR 基准会把所有广告判成及格，必须拦住。"""
        res = config.update_overrides({"ads.ctr_bench": 0.99})
        self.assertIn("ads.ctr_bench", res["rejected"])
        self.assertAlmostEqual(config.load_config().get("ads.ctr_bench"), 0.02)

    def test_unknown_key_rejected(self):
        res = config.update_overrides({"not.a.real.key": 1})
        self.assertIn("not.a.real.key", res["rejected"])

    def test_null_resets_to_default(self):
        config.update_overrides({"ads.ctr_bench": 0.05})
        config.update_overrides({"ads.ctr_bench": None})
        self.assertEqual(config.load_config().source_of("ads.ctr_bench"), "default")

    def test_request_level_override_ignores_bad_value(self):
        """请求里传脏值不能污染判断，应静默回落到下一层。"""
        c = config.load_config("shopee_br", {"ads.ctr_bench": "abc"})
        self.assertEqual(c.source_of("ads.ctr_bench"), "platform")


class TestFormatting(unittest.TestCase):
    def test_kind_rendering(self):
        c = config.load_config()
        # 机会指数是倍数，不能显示成 120%
        self.assertEqual(config.THRESHOLDS["market.opportunity_blue"].fmt(1.2), "1.2")
        self.assertEqual(config.THRESHOLDS["ads.ctr_bench"].fmt(0.025), "2.5%")
        self.assertEqual(config.THRESHOLDS["video.chars_per_sec_zh"].fmt(4.5), "4.5")
        self.assertEqual(config.THRESHOLDS["ads.min_impressions"].fmt(1000), "1000")


class TestThresholdAffectsVerdict(unittest.TestCase):
    def test_ctr_threshold_changes_diagnosis(self):
        """同一个广告组，CTR 基准不同 → 诊断结论必须不同。

        这是阈值配置化存在的唯一理由：如果改了阈值结论不变，那配置就是摆设。
        """
        row = {
            "campaign": "配件组", "impressions": 12400, "clicks": 108,
            "cost": 38.2, "orders": 6, "revenue": 240.0, "price": 350,
        }
        m = M.funnel_row(row, price=350)

        strict = M.grade_campaign(m, breakeven_roas=4.3, target_roas=7.2, ctr_bench=0.025)
        loose = M.grade_campaign(m, breakeven_roas=4.3, target_roas=7.2, ctr_bench=0.005)

        self.assertEqual(strict["stage"], "click_through")   # 严：先怪创意
        self.assertEqual(loose["stage"], "below_target")     # 松：才轮到 ROAS

    def test_stuffing_limit_configurable(self):
        title = "patinete patinete patinete eletrico"
        strict = M.title_lint(title, 120, 30, ["patinete"], stuffing_limit=3)
        loose = M.title_lint(title, 120, 30, ["patinete"], stuffing_limit=4)
        self.assertFalse(strict["passed"])
        self.assertTrue(loose["passed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
