"""自动化层测试：cron 解析 + 闭环对比判定。

只测纯逻辑，不启线程、不写库 —— 调度器的集成测试在 scripts/demo_cycle.py 里覆盖。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import cycle
from app.core.cron import Cron, CronError, validate


class TestCron(unittest.TestCase):
    def test_daily(self):
        c = Cron("0 9 * * *")
        self.assertEqual(c.describe(), "每天 09:00")
        self.assertEqual(c.next_after(datetime(2026, 11, 7, 10, 30)), datetime(2026, 11, 8, 9, 0))

    def test_step_minutes(self):
        c = Cron("*/15 * * * *")
        self.assertEqual(c.describe(), "每 15 分钟")
        self.assertEqual(c.next_after(datetime(2026, 11, 7, 10, 31)), datetime(2026, 11, 7, 10, 45))

    def test_step_hours(self):
        c = Cron("0 */6 * * *")
        self.assertEqual(c.describe(), "每 6 小时")
        self.assertEqual(c.next_after(datetime(2026, 11, 7, 10, 30)), datetime(2026, 11, 7, 12, 0))

    def test_weekday(self):
        c = Cron("30 8 * * 1")
        nxt = c.next_after(datetime(2026, 11, 7, 10, 30))  # 2026-11-07 是周六
        self.assertEqual(nxt, datetime(2026, 11, 9, 8, 30))

    def test_describe_not_crash_on_step(self):
        """*/30 曾被当成数字 int() 而崩 —— 这是真实修过的 bug。"""
        for expr in ("*/30 * * * *", "*/5 * * * *", "0 */2 * * *"):
            ok, _ = validate(expr)
            self.assertTrue(ok, expr)

    def test_invalid(self):
        for expr in ("bad", "61 * * * *", "* * *", "*/0 * * * *"):
            with self.assertRaises(CronError):
                Cron(expr)

    def test_next_after_respects_month(self):
        c = Cron("0 0 1 12 *")  # 每年 12 月 1 日
        nxt = c.next_after(datetime(2026, 11, 7, 0, 0))
        self.assertEqual(nxt, datetime(2026, 12, 1, 0, 0))


def _result(roas, p0, good, bad, actions, p0_actions):
    """构造一个最小的运行结果，用于测试对比逻辑。"""
    return {
        "stages": [
            {"stage": "ads", "metrics": {"summary": {"overall_roas": roas, "p0_count": p0}}, "actions": []},
            {
                "stage": "review",
                "metrics": {"aggregate": {"good_rate": good, "bad_rate": bad}},
                "actions": [],
            },
            {
                "stage": "market",
                "actions": [
                    {"priority": "P0", "target": t, "change": "x", "reason": "", "verify": "", "effort": "低"}
                    for t in p0_actions
                ],
            },
        ],
        "summary": {"action_total": actions, "p0_total": len(p0_actions)},
        "run_id": "test",
    }


class TestCycle(unittest.TestCase):
    def test_snapshot_extracts_metrics(self):
        snap = cycle.snapshot(_result(5.0, 2, 0.86, 0.14, 29, ["A", "B"]))
        self.assertEqual(snap["ads_roas"], 5.0)
        self.assertEqual(snap["ads_p0"], 2)
        self.assertEqual(snap["review_bad_rate"], 0.14)
        self.assertEqual(snap["p0_total"], 2)

    def test_improvement(self):
        prev = cycle.current_payload(_result(5.0, 2, 0.86, 0.14, 29, ["A", "B"]))
        curr = cycle.current_payload(_result(8.0, 0, 0.92, 0.08, 27, []))
        rep = cycle.compare(prev, curr)
        self.assertEqual(rep["verdict"], "改善")
        roas = next(r for r in rep["rows"] if r["key"] == "ads_roas")
        self.assertEqual(roas["trend"], "better")

    def test_regression(self):
        prev = cycle.current_payload(_result(8.0, 0, 0.92, 0.08, 27, []))
        curr = cycle.current_payload(_result(5.0, 2, 0.86, 0.14, 29, ["A"]))
        rep = cycle.compare(prev, curr)
        self.assertEqual(rep["verdict"], "恶化")

    def test_bad_rate_direction(self):
        """差评率下降必须是改善 —— 方向搞反会给出完全相反的结论。"""
        prev = cycle.current_payload(_result(5.0, 2, 0.86, 0.20, 29, []))
        curr = cycle.current_payload(_result(5.0, 2, 0.86, 0.10, 29, []))
        rep = cycle.compare(prev, curr)
        row = next(r for r in rep["rows"] if r["key"] == "review_bad_rate")
        self.assertEqual(row["trend"], "better")

    def test_stale_action_detection(self):
        """上次的 P0 这次还在 → 说明没执行或没效果。"""
        prev = cycle.current_payload(_result(5.0, 2, 0.86, 0.14, 29, ["价格带 300-400", "主图"]))
        curr = cycle.current_payload(_result(5.0, 2, 0.86, 0.14, 29, ["价格带 300-400"]))
        rep = cycle.compare(prev, curr)
        self.assertEqual(len(rep["pending"]["stale"]), 1)
        self.assertEqual(rep["pending"]["stale"][0]["target"], "价格带 300-400")
        self.assertEqual(len(rep["pending"]["resolved"]), 1)
        self.assertEqual(rep["pending"]["resolved"][0]["target"], "主图")

    def test_first_run_without_baseline(self):
        rep = cycle.compare({}, cycle.current_payload(_result(5.0, 2, 0.86, 0.14, 29, [])))
        self.assertEqual(rep["verdict"], "首次运行")
        self.assertFalse(rep["has_baseline"])

    def test_integer_metrics_not_float(self):
        """整数指标不该显示成 2.0。"""
        prev = cycle.current_payload(_result(5.0, 2, 0.86, 0.14, 29, ["A"]))
        curr = cycle.current_payload(_result(5.0, 3, 0.86, 0.14, 30, ["A"]))
        rep = cycle.compare(prev, curr)
        row = next(r for r in rep["rows"] if r["key"] == "ads_p0")
        self.assertIsInstance(row["curr"], int)


if __name__ == "__main__":
    unittest.main(verbosity=2)
