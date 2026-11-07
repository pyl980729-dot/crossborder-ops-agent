"""时间轴测试。

重点不是「函数能不能跑」，而是三件会让结论反向的事：
1. 01/11 到底是 11 月 1 日还是 1 月 11 日
2. 「本期 3 天 vs 上期 7 天」这种不等长对比
3. 缺一天数据被当成 0

这三类错误都不会抛异常，只会安静地给出一个错误结论 —— 所以必须测。
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import metrics as M
from app.core import timeline


class TestDateParsing(unittest.TestCase):
    def test_iso_formats(self):
        for text in ("2026-11-07", "2026/11/07", "20261107", "2026.11.7"):
            self.assertEqual(timeline.parse_date(text), date(2026, 11, 7), text)

    def test_datetime_string(self):
        self.assertEqual(timeline.parse_date("2026-11-07 13:22:00"), date(2026, 11, 7))

    def test_chinese(self):
        self.assertEqual(timeline.parse_date("2026年11月7日"), date(2026, 11, 7))

    def test_portuguese_month(self):
        """巴西站导出常见 07/nov/2026 和 7 de novembro de 2026。"""
        self.assertEqual(timeline.parse_date("07/nov/2026"), date(2026, 11, 7))
        self.assertEqual(timeline.parse_date("7 de novembro de 2026"), date(2026, 11, 7))
        self.assertEqual(timeline.parse_date("07-out-2026"), date(2026, 10, 7))

    def test_english_month(self):
        self.assertEqual(timeline.parse_date("Nov 7, 2026"), date(2026, 11, 7))
        self.assertEqual(timeline.parse_date("March 3, 2026"), date(2026, 3, 3))

    def test_two_digit_year(self):
        self.assertEqual(timeline.parse_date("07/11/26"), date(2026, 11, 7))

    def test_invalid_returns_none(self):
        for text in ("", "oops", "2026-13-45", "31/02/2026", None):
            self.assertIsNone(timeline.parse_date(text), repr(text))

    def test_order_matters(self):
        """同一个字符串，两种日序解析出两个日期 —— 这就是必须检测的原因。"""
        self.assertEqual(timeline.parse_date("01/11/2026", order="dmy"), date(2026, 11, 1))
        self.assertEqual(timeline.parse_date("01/11/2026", order="mdy"), date(2026, 1, 11))

    def test_unambiguous_overrides_order(self):
        """出现 25 这种大于 12 的日子时，数据本身就能定序，order 不该起作用。"""
        self.assertEqual(timeline.parse_date("25/10/2026", order="mdy"), date(2026, 10, 25))


class TestOrderDetection(unittest.TestCase):
    def test_data_evidence_dmy(self):
        order, source = timeline.detect_order(["01/11/2026", "25/10/2026"])
        self.assertEqual((order, source), ("dmy", "data"))

    def test_data_evidence_mdy(self):
        order, source = timeline.detect_order(["11/01/2026", "10/25/2026"])
        self.assertEqual((order, source), ("mdy", "data"))

    def test_no_evidence(self):
        order, source = timeline.detect_order(["01/11/2026", "02/11/2026"])
        self.assertEqual((order, source), ("unknown", "no_evidence"))

    def test_conflict_is_not_guessed(self):
        """同一列里两种写法都出现，说明源文件有问题，必须报出来而不是猜。"""
        order, source = timeline.detect_order(["25/10/2026", "10/25/2026"])
        self.assertEqual((order, source), ("unknown", "conflict"))

    def test_iso_follows(self):
        """ISO 写法本来就没有歧义，不该被算成「有证据」。"""
        order, source = timeline.detect_order(["2026-11-07", "2026-11-08"])
        self.assertEqual((order, source), ("unknown", "no_evidence"))


def _rows(days: int, start: date = date(2026, 10, 1), **fields) -> list[dict]:
    return [
        {"date": (start + timedelta(days=i)).isoformat(), "campaign": "A", **fields}
        for i in range(days)
    ]


class TestWindow(unittest.TestCase):
    def test_windows_are_equal_length_and_adjacent(self):
        """核心约束：两个窗口必须等长且首尾相接。

        如果拿「本周 3 天」跟「上周 7 天」比，任何指标都会显示暴跌 ——
        这是运营日报里最常见的假结论。
        """
        rows = _rows(30)
        got = timeline.analyze(rows, window=7)

        self.assertTrue(got["available"])
        cur_end = date.fromisoformat(got["current"]["end"])
        cur_start = date.fromisoformat(got["current"]["start"])
        prev_end = date.fromisoformat(got["previous"]["end"])
        prev_start = date.fromisoformat(got["previous"]["start"])

        self.assertEqual((cur_end - cur_start).days + 1, 7)
        self.assertEqual((prev_end - prev_start).days + 1, 7)
        self.assertEqual(prev_end + timedelta(days=1), cur_start)  # 相邻，不重叠
        self.assertEqual(cur_end, date(2026, 10, 30))  # 锚在数据最后一天

    def test_not_enough_span(self):
        """只有 10 天数据时，7 天窗口的两期对比做不了 —— 要说清楚缺多少天。"""
        got = timeline.analyze(_rows(10), window=7)
        self.assertFalse(got["available"])
        self.assertIn("10 天", got["reason"])
        self.assertIn("14", got["reason"])

    def test_no_dates(self):
        got = timeline.analyze([{"campaign": "A"}], window=7)
        self.assertFalse(got["available"])
        self.assertIn("日期", got["reason"])

    def test_gap_lowers_coverage(self):
        """中间缺一天：可用，但要如实报出覆盖率，不能假装是 7 天完整数据。"""
        rows = [r for r in _rows(30) if r["date"] != "2026-10-27"]
        got = timeline.analyze(rows, window=7)
        self.assertTrue(got["available"])
        self.assertAlmostEqual(got["current"]["coverage"], 6 / 7, places=3)
        self.assertEqual(got["current"]["days_with_data"], 6)

    def test_low_coverage_blocks_comparison(self):
        """跨度够但窗口里几乎没数据时，涨跌反映的是「投放了几天」，不是效果好坏。

        构造：10-01 到 10-27 跨度 27 天（够），但只有 5 天有数据，
        本期窗口 7 天里只有 2 天、上期窗口一天都没有。
        """
        keep = {"2026-10-01", "2026-10-02", "2026-10-03", "2026-10-26", "2026-10-27"}
        rows = [r for r in _rows(30) if r["date"] in keep]
        got = timeline.analyze(rows, window=7, min_coverage=0.6)
        self.assertFalse(got["available"])
        self.assertIn("可用天数不足", got["reason"])

    def test_series_marks_missing_days(self):
        rows = [r for r in _rows(30) if r["date"] != "2026-10-20"]
        got = timeline.analyze(rows, window=7, series_fn=lambda rs: {"n": len(rs)})
        by_date = {p["date"]: p for p in got["series"]}
        self.assertFalse(by_date["2026-10-20"]["has_data"])
        self.assertEqual(by_date["2026-10-19"]["n"], 1)
        self.assertEqual(len(got["series"]), 30)

    def test_unparsed_rows_excluded(self):
        rows = _rows(30) + [{"date": "??", "campaign": "A"}]
        got = timeline.analyze(rows, window=7)
        self.assertEqual(got["rows_without_date"], 1)
        self.assertTrue(got["available"])

    def test_assumption_recorded(self):
        got = timeline.analyze(_rows(30), window=7, order="mdy")
        self.assertIn("月/日", got["assumption"])


class TestGroupDelta(unittest.TestCase):
    def test_only_campaigns_in_both_windows(self):
        """上期没有的组不算涨跌 —— 从 0 暴涨是噪声，不是趋势。"""
        cur = [{"campaign": "A", "cost": 2}] + [{"campaign": "B", "cost": 5}]
        prev = [{"campaign": "A", "cost": 1}]
        out = timeline.group_delta(cur, prev, key="campaign", value_fn=lambda rs: sum(r["cost"] for r in rs))
        self.assertEqual([c["name"] for c in out], ["A"])
        self.assertAlmostEqual(out[0]["delta_pct"], 1.0)


class TestCollapseByCampaign(unittest.TestCase):
    def test_daily_rows_become_one(self):
        """日报里一个组有 7 行，必须先合并再诊断。

        第一版没合并，6 个组被逐行分级成了 42 个组，
        P0 数量直接放大 7 倍 —— 而「组数」正是给人看的核心指标。
        """
        rows = [
            {"date": f"2026-11-0{i}", "campaign": "A", "impressions": 1000, "clicks": 50,
             "cost": 10.0, "orders": 2, "revenue": 700.0}
            for i in range(1, 8)
        ]
        merged = M.collapse_by_campaign(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["impressions"], 7000)
        self.assertEqual(merged[0]["orders"], 14)
        self.assertAlmostEqual(merged[0]["cost"], 70.0)

        report = M.diagnose_ads(
            rows, breakeven_roas=4.3, target_roas=7.2, ctr_bench=0.02,
        )
        self.assertEqual(report["summary"]["campaign_count"], 1)

    def test_ratios_are_recomputed_not_averaged(self):
        """合并后 ROAS 必须由总额重算，不能对每天的 ROAS 取平均。"""
        rows = [
            {"campaign": "A", "impressions": 1000, "clicks": 50, "cost": 10.0, "orders": 1, "revenue": 100.0},
            {"campaign": "A", "impressions": 1000, "clicks": 50, "cost": 10.0, "orders": 9, "revenue": 900.0},
        ]
        report = M.diagnose_ads(rows, breakeven_roas=4.3, target_roas=7.2, ctr_bench=0.02)
        self.assertAlmostEqual(report["campaigns"][0]["roas"], 50.0)  # 1000/20


class TestAgentIntegration(unittest.TestCase):
    """接进 Agent 之后的行为：有日期走趋势，没日期必须说清楚为什么没有。"""

    @classmethod
    def setUpClass(cls):
        from app.demo import build_demo_payload
        from app.orchestrator import run_single

        cls.payload = build_demo_payload()
        cls.with_dates = run_single("ads", cls.payload)

        undated = {k: v for k, v in cls.payload.items()}
        undated["campaigns"] = [
            {k: v for k, v in c.items() if k != "date"} for c in cls.payload["campaigns"]
        ]
        cls.without_dates = run_single("ads", undated)

    def test_available_with_dates(self):
        tl = self.with_dates["metrics"]["timeline"]
        self.assertTrue(tl["available"])
        self.assertTrue(tl["current"]["start"] < tl["current"]["end"])

    def test_diagnosis_uses_recent_window(self):
        """有日期时诊断范围是「近 N 天」，不是整表 —— 否则 90 天的平均会把上周的崩坏平均掉。"""
        self.assertIn("近 7 天", self.with_dates["headline"])
        self.assertIn("全表汇总", self.without_dates["headline"])

    def test_unavailable_is_explained(self):
        tl = self.without_dates["metrics"]["timeline"]
        self.assertFalse(tl["available"])
        self.assertTrue(tl["reason"])
        # 没有趋势时必须有一条洞察说明原因，不能静默省略
        self.assertTrue(
            any(i["evidence"] == "timeline.unavailable" for i in self.without_dates["insights"])
        )

    def test_raw_rows_not_leaked(self):
        """current/previous 里的原始行不能进 JSON，否则响应会大十几倍。"""
        tl = self.with_dates["metrics"]["timeline"]
        self.assertNotIn("rows", tl["current"])
        self.assertNotIn("rows", tl["previous"])

    def test_p0_count_compared(self):
        """P0 组数前后对比是闭环最硬的证据，必须出现在对比行里。"""
        tl = self.with_dates["metrics"]["timeline"]
        self.assertIn("p0_count", [r["key"] for r in tl["change"]])

    def test_review_uses_longer_window(self):
        """评论的窗口必须比广告长：评论一天才几条，按周看全是噪声。"""
        from app.core.config import load_config

        cfg = load_config()
        self.assertGreater(
            cfg.get_int("timeline.review_window_days"),
            cfg.get_int("timeline.ads_window_days"),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
