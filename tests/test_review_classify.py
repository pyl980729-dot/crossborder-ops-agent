"""评论自动归因测试。

最重要的一组是「语义反转」：字面命中某个参数词，
但用户真正说的是「我的预期错了」—— 这两类的补救动作完全相反。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.review_classify import (
    CATEGORY_PRIORITY,
    PENDING,
    classify_batch,
    classify_by_rules,
)


class TestRuleClassify(unittest.TestCase):
    def test_strong_signal_beats_keyword_hit(self):
        """「电池没我预期的久」必须判误解型，不能因为出现 bateria 就判参数不符。

        误解型 → 改详情页；参数不符 → 改产品。判错方向，动作就做反了。
        """
        cat, hits, words = classify_by_rules(
            "Usei e a bateria durou menos que o esperado. Eu esperava mais."
        )
        self.assertEqual(cat, "误解型")
        self.assertIn("esperava", words[0])

    def test_chinese_misunderstanding(self):
        cat, _, _ = classify_by_rules("我以为里面带充电器的，结果没有")
        self.assertEqual(cat, "误解型")

    def test_missing_accessory_strong(self):
        cat, _, _ = classify_by_rules("Faltou o carregador na caixa.")
        self.assertEqual(cat, "配件缺失")

    def test_broken_strong(self):
        cat, _, _ = classify_by_rules("Chegou com a roda traseira amassada.")
        self.assertEqual(cat, "破损/故障/退货")

    def test_english_delay(self):
        cat, _, _ = classify_by_rules("Shipping took almost a month, very slow.")
        self.assertEqual(cat, "发货慢")

    def test_generic_parameter(self):
        cat, _, _ = classify_by_rules("A potência não parece 500W, sobe ladeira com dificuldade.")
        self.assertEqual(cat, "参数不符合预期")

    def test_empty_text(self):
        cat, hits, _ = classify_by_rules("")
        self.assertEqual(cat, "")
        self.assertEqual(hits, 0)

    def test_unknown_text_returns_empty(self):
        cat, _, _ = classify_by_rules("ok")
        self.assertEqual(cat, "")


class TestBatch(unittest.TestCase):
    def test_manual_labels_preserved(self):
        """人工已经标过的不覆盖 —— 人的判断优先于机器。"""
        reviews = [{"rating": 1, "text": "坏了一个月", "category": "发货慢"}]
        out, stats = classify_batch(reviews, use_llm=False)
        self.assertEqual(out[0]["category"], "发货慢")
        self.assertEqual(stats["manual"], 1)
        self.assertEqual(stats["auto"], 0)

    def test_unlabeled_gets_classified(self):
        reviews = [
            {"rating": 1, "text": "Chegou quebrado, parou de funcionar no primeiro dia."},
            {"rating": 2, "text": "Faltou o manual na caixa."},
        ]
        out, stats = classify_batch(reviews, use_llm=False)
        self.assertEqual(out[0]["category"], "破损/故障/退货")
        self.assertEqual(out[1]["category"], "配件缺失")
        self.assertEqual(stats["coverage"], 1.0)

    def test_pending_when_unclassifiable(self):
        """分不出来就说不分，不许硬塞一个类别。"""
        reviews = [{"rating": 2, "text": "hmm"}]
        out, stats = classify_batch(reviews, use_llm=False)
        self.assertEqual(out[0]["category"], PENDING)
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["coverage"], 0.0)

    def test_confidence_levels(self):
        reviews = [
            {"rating": 1, "text": "Chegou quebrado, amassado, parou de funcionar."},  # 3+ 命中
            {"rating": 2, "text": "Achei que vinha com bateria extra."},               # 强信号
            {"rating": 2, "text": "Produto caro demais."},                             # 1 命中
        ]
        out, _ = classify_batch(reviews, use_llm=False)
        self.assertIn(out[0]["classify_confidence"], (0.6, 0.75, 0.9))
        self.assertEqual(out[1]["category"], "误解型")
        self.assertEqual(out[2]["classify_confidence"], 0.6)  # 低置信度，会被提示抽查

    def test_all_categories_reachable(self):
        """八类都必须有能被命中的表达，否则那个类别形同虚设。"""
        samples = {
            "破损/故障/退货": "Veio quebrado",
            "配件缺失": "Faltou o cabo",
            "参数不符合预期": "A autonomia é menor que o anunciado",
            "描述不符": "A cor é diferente da foto",
            "误解型": "Achei que era maior",
            "使用困难": "Difícil de montar",
            "发货慢": "Demorou muito para chegar",
            "价格偏高": "Muito caro pelo que oferece",
        }
        for expected, text in samples.items():
            cat, _, _ = classify_by_rules(text)
            self.assertEqual(cat, expected, f"{text!r} 应判 {expected}，实际 {cat}")

    def test_empty_list(self):
        out, stats = classify_batch([], use_llm=False)
        self.assertEqual(out, [])
        self.assertEqual(stats["total"], 0)
        self.assertEqual(stats["coverage"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
