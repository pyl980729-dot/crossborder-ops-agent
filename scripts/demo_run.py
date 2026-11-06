"""端到端演示 —— 读种子数据跑完整条流水线，并打印可读报告。

用法：
    python scripts/demo_run.py            # 跑全链路
    python scripts/demo_run.py --json     # 输出原始 JSON（接前端调试用）
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.demo import build_demo_payload  # noqa: E402
from app.orchestrator import run_stages  # noqa: E402

SEED = ROOT / "data" / "seed"


def print_report(result: dict) -> None:
    line = "=" * 72
    print(f"\n{line}\n运行 {result['run_id']} · {result['title']}\n{line}")
    print(f"状态 {result['status']}　耗时 {result['elapsed_ms']}ms　"
          f"动作 {result['summary']['action_total']} 条（P0 {result['summary']['p0_total']}）　"
          f"回流 {result['summary']['backflow_count']} 条\n")

    for stage in result["stages"]:
        mark = {"ok": "OK  ", "partial": "PART", "blocked": "STOP"}[stage["status"]]
        print(f"[{mark}] {stage['title']}")
        print(f"       {stage['headline']}")
        if stage["missing"]:
            print(f"       缺失字段: {', '.join(stage['missing'])}")
        for ins in stage["insights"][:3]:
            icon = {"good": "+", "info": "-", "warn": "!", "risk": "x"}[ins["level"]]
            print(f"       {icon} {ins['text']}")
        for act in stage["actions"][:2]:
            print(f"       [{act['priority']}] {act['target']} → {act['change']}")
        print()

    if result["backflow"]:
        print("---- 闭环回流 ----")
        for fb in result["backflow"]:
            print(f"  {fb['from_stage']} → {fb['to_stage']}（{fb['count']} 条）：{fb['scope']}")
    print()


if __name__ == "__main__":
    db.init_db()
    payload = build_demo_payload()
    result = run_stages(payload)
    if "--json" in sys.argv:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_report(result)
