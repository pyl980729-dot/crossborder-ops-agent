"""数据接入测试 —— 真实 CSV 的脏数据是这一层的主要敌人。

重点覆盖三类真实问题：
1. 中文 Excel 导出的 GBK 编码
2. 「R$ 420,50」这种欧式小数逗号
3. 列名千奇百怪（曝光量 / impressions / 展示次数）
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.datasets import DatasetError, _to_number, build_payload, parse_csv


def csv_bytes(rows: list[list[str]], encoding: str = "utf-8") -> bytes:
    """用 csv.writer 生成 —— 字段里含逗号（如 R$ 420,50）会自动加引号。"""
    import csv as _csv

    buf = io.StringIO()
    _csv.writer(buf).writerows(rows)
    return buf.getvalue().encode(encoding)


class TestNumberParsing(unittest.TestCase):
    def test_european_decimal_comma(self):
        """R$ 420,50 曾被解析成 42050 —— 这是真实修过的 bug。"""
        self.assertAlmostEqual(_to_number("R$ 420,50"), 420.50, places=2)
        self.assertAlmostEqual(_to_number("186,40"), 186.40, places=2)
        self.assertAlmostEqual(_to_number("240,00"), 240.00, places=2)

    def test_thousand_separators(self):
        self.assertAlmostEqual(_to_number("R$ 4.200,00"), 4200.00, places=2)
        self.assertAlmostEqual(_to_number("1,234.56"), 1234.56, places=2)
        self.assertAlmostEqual(_to_number("1.234,56"), 1234.56, places=2)
        self.assertAlmostEqual(_to_number("1,234"), 1234.00, places=2)
        self.assertAlmostEqual(_to_number("R$ 1.234.567,89"), 1234567.89, places=2)

    def test_garbage(self):
        for v in ("", "-", "abc", None):
            self.assertIsNone(_to_number(v))


class TestColumnMapping(unittest.TestCase):
    def test_chinese_headers_gbk(self):
        """中文列名 + GBK 编码，这是国内卖家最常见的导出格式。"""
        raw = csv_bytes(
            [
                ["广告名称", "曝光量", "点击量", "花费(BRL)", "订单数", "销售额"],
                ["A组", "48200", "1310", "R$ 420,50", "12", "R$ 4.200,00"],
            ],
            encoding="gbk",
        )
        res = parse_csv(raw, "ads")
        self.assertEqual(res["row_count"], 1)
        row = res["rows"][0]
        self.assertEqual(row["campaign"], "A组")
        self.assertEqual(row["impressions"], 48200)
        self.assertIsInstance(row["impressions"], int)
        self.assertAlmostEqual(row["cost"], 420.50, places=2)

    def test_english_headers(self):
        raw = csv_bytes(
            [["campaign", "impressions", "clicks", "cost", "orders", "revenue"],
             ["B", "10000", "300", "120.5", "9", "3150"]]
        )
        res = parse_csv(raw, "ads")
        self.assertEqual(res["row_count"], 1)
        self.assertEqual(res["rows"][0]["orders"], 9)

    def test_missing_required_column_reports_which(self):
        raw = csv_bytes([["广告名称", "曝光量"], ["A组", "100"]])
        with self.assertRaises(DatasetError) as ctx:
            parse_csv(raw, "ads")
        msg = str(ctx.exception)
        self.assertIn("缺少必填列", msg)
        self.assertIn("点击", msg)  # 要具体说是哪列，不能只说格式错误

    def test_ignored_columns_reported(self):
        raw = csv_bytes(
            [["campaign", "impressions", "clicks", "cost", "orders", "revenue", "备注"],
             ["A", "1000", "50", "10", "1", "350", "随便写"]]
        )
        res = parse_csv(raw, "ads")
        self.assertIn("备注", res["ignored_columns"])

    def test_bad_rows_skipped_not_silently_zeroed(self):
        raw = csv_bytes(
            [["campaign", "impressions", "clicks", "cost", "orders", "revenue"],
             ["好行", "1000", "50", "10", "1", "350"],
             ["坏行", "abc", "50", "10", "1", "350"]]
        )
        res = parse_csv(raw, "ads")
        self.assertEqual(res["row_count"], 1)
        self.assertEqual(res["skipped"], 1)
        self.assertTrue(res["warnings"])

    def test_empty_file(self):
        with self.assertRaises(DatasetError):
            parse_csv(b"", "ads")


class TestBuildPayload(unittest.TestCase):
    def test_missing_dataset_not_fabricated(self):
        """只有广告数据时，不能凭空造一份商品表。"""
        payload = build_payload(ads=[{"campaign": "A"}], meta={"category": "测试"})
        self.assertIn("campaigns", payload)
        self.assertNotIn("products", payload)
        self.assertNotIn("reviews", payload)

    def test_meta_passthrough(self):
        payload = build_payload(
            products=[{"price": 100}],
            meta={"category": "C", "product_cost": 50, "target_price": 100},
        )
        self.assertEqual(payload["product_cost"], 50)
        self.assertEqual(payload["target_price"], 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
