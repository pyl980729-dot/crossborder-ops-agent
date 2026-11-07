"""闭环优化演示 —— 三轮模拟：基线 → 执行动作 → 再优化。

用同一份种子数据派生三次运行，模拟真实运营节奏：
  第 1 轮：拿到诊断，9 条 P0 动作
  第 2 轮：执行了「关停失血广告组 + 修补文案」，复测指标改善
  第 3 轮：新开一组投放但效果差，指标部分恶化

用法：
    python scripts/demo_cycle.py            # 清空历史后跑三轮
    python scripts/demo_cycle.py --keep     # 保留历史，接着最近的记录比
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.demo import build_demo_payload  # noqa: E402
from app.orchestrator import run_stages  # noqa: E402

LINE = "═" * 78


def round1() -> dict:
    """基线：原始数据。"""
    return build_demo_payload()


def round2() -> dict:
    """执行 P0 动作后：关停两个失血组 + 修补误解型文案。"""
    p = copy.deepcopy(build_demo_payload())

    # 动作一：关停 P0 失血组
    p["campaigns"] = [
        c
        for c in p["campaigns"]
        if c["campaign"] not in ("BR-Patinete-低价引流", "BR-Scooter-竞品词")
    ]

    # 动作二：详情页补注意事项，误解型差评被消掉
    fixed = 0
    for review in p["reviews"]:
        if review.get("category") == "误解型" and fixed < 9:
            review["rating"] = 5.0
            review["category"] = "未标注"
            fixed += 1
    return p


def round3() -> dict:
    """第二轮优化：新开一组场景词投放，但素材没跟上，效果很差。"""
    p = round2()
    p["campaigns"] = list(p["campaigns"]) + [
        {
            "campaign": "BR-新测试-场景词",
            "impressions": 16400,
            "clicks": 231,
            "cost": 196.40,
            "orders": 1,
            "revenue": 350.00,
        }
    ]
    return p


def show(title: str, result: dict) -> None:
    print(f"\n{LINE}\n{title}\n{LINE}")
    s = result["summary"]
    print(f"状态 {result['status']}　动作 {s['action_total']} 条（P0 {s['p0_total']}）　回流 {s['backflow_count']} 条")

    cyc = result.get("cycle") or {}
    print(f"\n闭环判定：{cyc.get('verdict', '—')}　{cyc.get('reason', '')}")
    if cyc.get("baseline_run_id"):
        print(f"对比基线：{cyc['baseline_run_id']}（{cyc.get('baseline_at', '')}）")

    rows = cyc.get("rows") or []
    if rows:
        arrow = {"better": "↑ 改善", "worse": "↓ 恶化", "same": "= 持平"}
        print()
        for r in rows:
            delta = f"{r['delta']:+}" if r["delta"] else "0"
            print(f"  {r['label']:<14} {str(r['prev']):>8} → {str(r['curr']):>8}  {delta:>8}  {arrow[r['trend']]}")

    pending = cyc.get("pending") or {}
    if pending.get("stale"):
        print(f"\n  仍存在的 P0（说明动作没执行或没生效）：")
        for a in pending["stale"][:5]:
            print(f"    · {a['target']}　{a['change'][:40]}")
    if pending.get("resolved"):
        print(f"\n  已消除的 P0：{len(pending['resolved'])} 条")
        for a in pending["resolved"][:5]:
            print(f"    ✓ {a['target']}")

    ads = next((st for st in result["stages"] if st["stage"] == "ads"), None)
    if ads and ads["metrics"].get("summary"):
        sm = ads["metrics"]["summary"]
        print(f"\n  广告：花费 {sm['total_cost']} / 收入 {sm['total_revenue']} / ROAS {sm['overall_roas']} / P0 {sm['p0_count']} 组")


if __name__ == "__main__":
    db.init_db()
    keep = "--keep" in sys.argv
    if not keep:
        import sqlite3

        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("DELETE FROM runs")
        conn.commit()
        conn.close()
        print("已清空历史运行记录（用 --keep 可保留）")

    for idx, (label, builder) in enumerate(
        [
            ("第 1 轮 · 基线诊断", round1),
            ("第 2 轮 · 执行「关停失血组 + 修补文案」后复测", round2),
            ("第 3 轮 · 新开一组投放但素材没跟上", round3),
        ],
        start=1,
    ):
        result = run_stages(builder())
        show(label, result)

    print(f"\n{LINE}")
    print("三轮跑完。打开 http://127.0.0.1:8000 的「自动化」页可以看到同样的闭环对比。")
