"""Stage 基类 —— 所有 Agent 共享的行为。

每个 Agent 的固定套路（顺序不能变）：
  1. 校验契约 → 缺字段立刻 blocked，不进入计算
  2. 调 metrics 算数 → 得到确定性的 metrics
  3. 读 metrics 出解读 → 规则驱动，可复现
  4. 出动作清单 → 每条带验证方式
  5. 需要回流就发 feedback → 指向上游 Stage
"""

from __future__ import annotations

import time
from typing import Any

from app.core.contracts import (
    STAGE_CONTRACTS,
    Action,
    Feedback,
    Insight,
    StageResult,
    missing_fields,
)


class StageAgent:
    key: str = ""
    title: str = ""

    def __init__(self) -> None:
        spec = STAGE_CONTRACTS.get(self.key, {})
        self.title = spec.get("title", self.title)
        self.required: list[str] = spec.get("required", [])

    # ---- 契约 ----
    def check(self, payload: dict) -> list[str]:
        return missing_fields(payload, self.required)

    def blocked(self, missing: list[str], hint: str = "") -> StageResult:
        return StageResult(
            stage=self.key,
            title=self.title,
            status="blocked",
            headline=f"[INPUT_MISSING] 缺少 {len(missing)} 个必填字段，已停止计算（不猜数据）",
            missing=missing,
            assumptions=[hint] if hint else [],
            evidence=[f"contract:{self.key}.required={','.join(self.required)}"],
        )

    # ---- 执行 ----
    def run(self, payload: dict) -> StageResult:
        started = time.perf_counter()
        missing = self.check(payload)
        if missing:
            return self.blocked(missing)

        try:
            result = self._run(payload)
        except Exception as exc:  # 契约之外的计算失败也要可读，不能 500
            result = StageResult(
                stage=self.key,
                title=self.title,
                status="blocked",
                headline=f"计算失败：{type(exc).__name__}: {exc}",
                missing=[],
            )
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)  # type: ignore[attr-defined]
        return result

    def _run(self, payload: dict) -> StageResult:  # pragma: no cover - 子类实现
        raise NotImplementedError


# --------------------------------------------------------------------------
# 构造小工具，让子类代码短一点
# --------------------------------------------------------------------------

def insight(text: str, level: str = "info", evidence: str = "") -> Insight:
    return Insight(text=text, level=level, evidence=evidence)  # type: ignore[arg-type]


def action(
    priority: str,
    target: str,
    change: str,
    reason: str,
    verify: str = "",
    effort: str = "低",
) -> Action:
    return Action(
        priority=priority,  # type: ignore[arg-type]
        target=target,
        change=change,
        reason=reason,
        verify=verify,
        effort=effort,  # type: ignore[arg-type]
    )


def feedback(to_stage: str, reason: str, scope: str) -> Feedback:
    return Feedback(to_stage=to_stage, reason=reason, scope=scope)


def fmt_money(value: float, currency: str = "R$") -> str:
    return f"{currency} {value:,.2f}"


def fmt_pct(value: float, digits: int = 1) -> str:
    return f"{value * 100:.{digits}f}%"
