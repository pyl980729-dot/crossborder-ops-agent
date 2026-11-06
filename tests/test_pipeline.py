"""流水线与契约测试 —— 验证「缺数据就阻塞」这条铁律真的生效。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db
from app.demo import build_demo_payload
from app.orchestrator import run_single, run_stages

db.init_db()


class TestContracts(unittest.TestCase):
    def test_missing_field_blocks(self):
        """缺 campaign 时广告 Stage 必须 blocked，而不是编一份报表出来。"""
        res = run_single("ads", {"target_price": 350, "product_cost": 168})
        self.assertEqual(res["status"], "blocked")
        self.assertIn("campaigns", res["missing"])

    def test_empty_value_counts_as_missing(self):
        res = run_single("market", {"category": "", "products": []})
        self.assertEqual(res["status"], "blocked")
        self.assertIn("category", res["missing"])

    def test_blocked_stage_has_no_fabricated_metrics(self):
        res = run_single("review", {})
        self.assertEqual(res["status"], "blocked")
        self.assertEqual(res["metrics"], {})
        self.assertEqual(res["insights"], [])


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = build_demo_payload()
        cls.result = run_stages(cls.payload)

    def test_all_stages_run(self):
        self.assertEqual(len(self.result["stages"]), 7)
        self.assertEqual(self.result["status"], "ok")

    def test_every_stage_has_traceable_evidence(self):
        for st in self.result["stages"]:
            self.assertTrue(st["evidence"], f"{st['stage']} 缺少溯源标记")

    def test_actions_have_verification(self):
        for st in self.result["stages"]:
            for a in st["actions"]:
                if a["priority"] == "P0":
                    self.assertTrue(a["verify"], f"{st['stage']} 的 P0 动作没有验证方式")

    def test_backflow_points_upstream(self):
        targets = {(b["from_stage"], b["to_stage"]) for b in self.result["backflow"]}
        self.assertTrue(targets, "闭环回流不能为空")
        for frm, to in targets:
            self.assertIn(to, ("listing", "image", "strategy"))

    def test_same_input_same_output(self):
        """同一份输入跑两次，结论必须逐字一致 —— 这是可复现的底线。"""
        again = run_stages(build_demo_payload())
        a = [s["headline"] for s in self.result["stages"]]
        b = [s["headline"] for s in again["stages"]]
        self.assertEqual(a, b)

    def test_ads_threshold_consistent_with_strategy(self):
        """02 与 06 的盈亏线必须是同一个数，否则动作清单会自相矛盾。"""
        strategy = next(s for s in self.result["stages"] if s["stage"] == "strategy")
        ads = next(s for s in self.result["stages"] if s["stage"] == "ads")
        self.assertAlmostEqual(
            strategy["metrics"]["pricing"]["breakeven_roas"],
            ads["metrics"]["thresholds"]["breakeven_roas"],
            places=2,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
