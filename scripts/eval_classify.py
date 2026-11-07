"""评论自动归因效果评估。

做法：拿种子数据里的人工标注当标准答案，把 category 抹掉让系统重分，
再逐条比对。这比「看起来挺准」有说服力。

用法：
    python scripts/eval_classify.py            # 只用规则（离线）
    python scripts/eval_classify.py --llm      # 规则 + 模型兜底
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.demo import build_demo_payload  # noqa: E402
from app.core.review_classify import classify_batch  # noqa: E402

LINE = "─" * 70


def main() -> int:
    use_llm = "--llm" in sys.argv
    reviews = build_demo_payload()["reviews"]

    # 只评估差评：好评本来就没有归因
    bad = [r for r in reviews if float(r["rating"]) < 3]
    truth = [str(r.get("category") or "").strip() for r in bad]

    # 抹掉人工标注，模拟真实导出的评论表（只有评分和正文）
    stripped = [{**r, "category": ""} for r in bad]
    predicted, stats = classify_batch(stripped, use_llm=use_llm)

    print(f"评估样本：{len(bad)} 条差评（人工标注作为标准答案）")
    print(f"分类方式：{'规则 + 模型兜底' if use_llm else '仅关键词规则'}")
    print()

    detail = []
    correct = 0
    for t, p in zip(truth, predicted):
        pred = p["category"]
        ok = pred == t
        correct += ok
        detail.append((t, pred, ok, p.get("classify_method", ""), p.get("classify_evidence", "")))

    total = len(detail)
    pending = sum(1 for d in detail if d[1] == "待人工确认")
    judged = total - pending
    acc = correct / total if total else 0
    acc_judged = correct / judged if judged else 0

    print(f"{'标准答案':<16}{'系统判定':<16}{'':<4}{'方式':<8}{'依据'}")
    print(LINE)
    for t, pred, ok, method, ev in detail:
        mark = "✓" if ok else "✗"
        print(f"{t or '(空)':<16}{pred:<16}{mark:<4}{method:<8}{ev}")

    print()
    print(f"覆盖率：{stats['coverage']:.1%}（自动分掉 {stats['auto']}/{stats['total']} 条）")
    print(f"准确率（含待确认按错算）：{acc:.1%}")
    print(f"准确率（只算已判定的 {judged} 条）：{acc_judged:.1%}")
    print(f"待人工确认：{pending} 条")
    print(f"方式分布：规则-高置信 {stats['rule_high']} / 中 {stats['rule_medium']} / "
          f"低 {stats['rule_low']} / 模型 {stats['llm']}")

    # 混淆情况
    wrong = [(t, p) for t, p, ok, *_ in detail if not ok]
    if wrong:
        print()
        print("错分情况：")
        for (t, p), n in Counter(wrong).most_common():
            print(f"  真值「{t}」→ 判成「{p}」  ×{n}")
    else:
        print()
        print("全部正确。")

    return 0 if acc_judged >= 0.85 else 1


if __name__ == "__main__":
    raise SystemExit(main())
