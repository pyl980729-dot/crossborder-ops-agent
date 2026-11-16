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
from app.core.logging import get_logger

log = get_logger("stage")


class StageAgent:
    """Stage 基类：先校验契约，再执行子类逻辑，最后统一计时与兜底异常。"""
    key: str = ""
    title: str = ""

    def __init__(self) -> None:
        """从 STAGE_CONTRACTS 读标题与必填字段，子类只需声明 key。"""
        spec = STAGE_CONTRACTS.get(self.key, {})
        self.title = spec.get("title", self.title)
        self.required: list[str] = spec.get("required", [])

    # ---- 契约 ----
    def check(self, payload: dict) -> list[str]:
        """返回缺失的必填字段列表；空字符串 / None / 空容器都算缺失。"""
        return missing_fields(payload, self.required)

    def blocked(self, missing: list[str], hint: str = "") -> StageResult:
        """把缺字段转成 status=blocked 的结果 —— 不填默认值，直接停止计算。"""
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
        """模板方法：check → _run → 计时；计算异常也转成可读的 blocked，不让接口 500。

        每个 Stage 的运行结果都在这里落一条日志 —— 七个模块共用同一个埋点，
        格式统一，排查时按 stage=xxx 过滤即可。
        """
        started = time.perf_counter()
        missing = self.check(payload)
        if missing:
            result = self.blocked(missing)
            result.elapsed_ms = int((time.perf_counter() - started) * 1000)
            log.warning(
                "stage=%s status=blocked elapsed_ms=%d missing=%s",
                self.key, result.elapsed_ms, ",".join(missing),
            )
            return result

        try:
            result = self._run(payload)
        except Exception as exc:  # 契约之外的计算失败也要可读，不能 500
            log.exception("stage=%s status=error", self.key)
            result = StageResult(
                stage=self.key,
                title=self.title,
                status="blocked",
                headline=f"计算失败：{type(exc).__name__}: {exc}",
                missing=[],
            )
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        log.info(
            "stage=%s status=%s elapsed_ms=%d insights=%d actions=%d p0=%d feedback=%d | %s",
            self.key,
            result.status,
            result.elapsed_ms,
            len(result.insights),
            len(result.actions),
            sum(1 for a in result.actions if a.priority == "P0"),
            len(result.feedback),
            (result.headline or "").replace("\n", " ")[:120],
        )
        return result

    def _run(self, payload: dict) -> StageResult:  # pragma: no cover - 子类实现
        """子类实现：只做「取数 → 调 metrics → 写文案 → 出动作」，不在这里算业务数字。"""
        raise NotImplementedError


# --------------------------------------------------------------------------
# 构造小工具，让子类代码短一点
# --------------------------------------------------------------------------

def insight(text: str, level: str = "info", evidence: str = "") -> Insight:
    """构造一条解读；evidence 必须指回 metrics 里的具体键，保证可审计。"""
    return Insight(text=text, level=level, evidence=evidence)  # type: ignore[arg-type]


def action(
    priority: str,
    target: str,
    change: str,
    reason: str,
    verify: str = "",
    effort: str = "低",
) -> Action:
    """构造一条动作；verify 是验证方式 —— 没有验证方式的动作清单等于没有。"""
    return Action(
        priority=priority,  # type: ignore[arg-type]
        target=target,
        change=change,
        reason=reason,
        verify=verify,
        effort=effort,  # type: ignore[arg-type]
    )


def feedback(to_stage: str, reason: str, scope: str) -> Feedback:
    """构造一条回流指令：下游诊断指回上游哪个环节、改什么。"""
    return Feedback(to_stage=to_stage, reason=reason, scope=scope)


def fmt_money(value: float, currency: str = "R$") -> str:
    """金额格式化，默认巴西雷亚尔。"""
    return f"{currency} {value:,.2f}"


def fmt_pct(value: float, digits: int = 1) -> str:
    """比率转百分比文本。"""
    return f"{value * 100:.{digits}f}%"
