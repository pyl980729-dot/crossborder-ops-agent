"""Stage 契约层 —— 每个 Agent 的输入输出都必须过这里。

核心机制：**输入契约化**。每个 Stage 声明它必须拿到哪些字段，
缺了就返回 status=blocked + missing 列表，绝不猜、绝不填默认值。

为什么这条比 Prompt 写得漂亮更重要：
LLM 的失败模式不是「不会写」，而是「缺数据时编一个像样的」。
把缺字段变成硬阻塞，幻觉就没有发生的位置。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

StageStatus = Literal["ok", "partial", "blocked"]
Level = Literal["good", "info", "warn", "risk"]
Priority = Literal["P0", "P1", "P2"]


# --------------------------------------------------------------------------
# 通用输出结构
# --------------------------------------------------------------------------

class Insight(BaseModel):
    """一条解读。evidence 必须指回 metrics 里的具体键，保证可审计。"""

    text: str
    level: Level = "info"
    evidence: str = ""


class Action(BaseModel):
    """一条动作。没有 verify 的动作清单等于没有。"""

    priority: Priority
    target: str
    change: str
    reason: str
    verify: str = ""
    effort: Literal["低", "中", "高"] = "低"


class Feedback(BaseModel):
    """回流指令：下游诊断指回上游哪个环节要改。"""

    to_stage: str
    reason: str
    scope: str


class StageResult(BaseModel):
    """所有 Stage 的统一返回体。前端只认这一种结构。"""

    stage: str
    title: str
    status: StageStatus = "ok"
    headline: str = ""
    metrics: dict[str, Any] = Field(default_factory=dict)
    insights: list[Insight] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    feedback: list[Feedback] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    next_stage: str | None = None
    elapsed_ms: int = 0


class ContractError(ValueError):
    """输入契约不满足。"""

    def __init__(self, stage: str, missing: list[str], hint: str = ""):
        self.stage = stage
        self.missing = missing
        self.hint = hint
        super().__init__(f"[INPUT_MISSING] {stage} 缺少字段: {', '.join(missing)}")


def missing_fields(payload: dict, required: list[str]) -> list[str]:
    """检查必填字段。空字符串、None、空列表都算缺失。"""
    out: list[str] = []
    for key in required:
        value = payload.get(key)
        if value is None or value == "" or (isinstance(value, (list, dict)) and len(value) == 0):
            out.append(key)
    return out


# --------------------------------------------------------------------------
# 各 Stage 的输入契约（字段名即对外 API 的约定）
# --------------------------------------------------------------------------

STAGE_CONTRACTS: dict[str, dict[str, Any]] = {
    "market": {
        "title": "01 市场调研",
        "required": ["category", "products"],
        "optional": ["platform"],
        "accepts": "类目 TOP 商品导出（sku/title/brand/price/monthly_sales/rating/config）",
        "produces": "容量、集中度、价格带机会、配置机会、进入结论",
    },
    "strategy": {
        "title": "02 选品与定价",
        "required": ["product_cost", "target_price"],
        "optional": ["commission_rate", "tax_rate", "return_rate", "shipping_cost", "target_margin"],
        "accepts": "成本、目标售价、费率结构",
        "produces": "净利率、盈亏线 ROAS、可承受 CPC、SKU 四角色矩阵",
    },
    "listing": {
        "title": "03 Listing 文案",
        "required": ["product_name", "selling_points", "keywords"],
        "optional": ["platform", "specs", "target_audience"],
        "accepts": "产品名、卖点、关键词、参数",
        "produces": "标题（含字符实测）、详情页结构、卖点排序",
    },
    "image": {
        "title": "04 主图与详情图",
        "required": ["product_name", "selling_points"],
        "optional": ["platform", "scenes"],
        "accepts": "产品名、主卖点",
        "produces": "九图规划、每图出图 Prompt、完整性校验",
    },
    "video": {
        "title": "05 短视频脚本",
        "required": ["product_name", "main_point"],
        "optional": ["platform", "duration", "audience"],
        "accepts": "产品名、唯一主卖点",
        "produces": "三幕脚本、分镜、口播稿、时长校验",
    },
    "ads": {
        "title": "06 广告诊断",
        "required": ["campaigns", "target_price", "product_cost"],
        "optional": ["commission_rate", "tax_rate", "ctr_bench", "target_margin"],
        "accepts": "广告导出报表（campaign/impressions/clicks/cost/orders/revenue）",
        "produces": "漏斗指标、P0/P1/P2 分级、动作清单、回流指令",
    },
    "review": {
        "title": "07 评论归因",
        "required": ["reviews"],
        "optional": ["sku"],
        "accepts": "评论导出（rating/text/category/has_image）",
        "produces": "八类归因、可修复率、修补建议、回流指令",
    },
}

STAGE_ORDER = ["market", "strategy", "listing", "image", "video", "ads", "review"]

# 闭环图：谁把结论回流给谁
BACKFLOW_MAP = {
    "ads": ["listing", "image", "strategy"],
    "review": ["listing", "image"],
}


def contract_catalog() -> list[dict]:
    """给前端/文档用的契约清单。"""
    return [
        {"stage": key, **value, "order": STAGE_ORDER.index(key)}
        for key, value in STAGE_CONTRACTS.items()
    ]
