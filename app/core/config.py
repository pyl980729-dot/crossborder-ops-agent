"""阈值配置 —— 所有可调判断参数集中在这里。

为什么必须做这件事：
「CTR 低于 2% 就是创意问题」这句话对 Shopee 和 Amazon 完全不成立 ——
Amazon 的广告 CTR 普遍在 0.4% 左右，用 2% 去卡会把所有正常广告判成失败。
阈值写死在代码里，等于把一个类目的经验当成通用真理。

优先级（从高到低）：
  1. 请求里显式传（payload.thresholds / payload 上的同名单值）
  2. 用户在界面上改过的覆盖值（存 settings 表）
  3. 平台覆盖（platforms.py 里按平台差异配置）
  4. 全局默认（本文件）

每一层都能查到来源，报告里会显示「这个阈值是哪来的」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app import db


@dataclass(frozen=True)
class ThresholdDef:
    key: str
    label: str
    group: str
    default: float
    kind: str  # ratio | count | money
    low: float
    high: float
    desc: str

    def fmt(self, value: float) -> str:
        if self.kind == "ratio":
            return f"{value * 100:.1f}%"
        if self.kind == "index":
            # 机会指数、失血系数是倍数不是比例 —— 显示成 120% 会让人误读
            return f"{value:.2f}".rstrip("0").rstrip(".")
        if self.kind == "decimal":
            return f"{value:g}"
        if self.kind == "money":
            return f"{value:.2f}"
        return f"{int(value)}"


# 顺序即前端展示顺序
THRESHOLDS: dict[str, ThresholdDef] = {
    # --- 广告诊断 ---
    "ads.ctr_bench": ThresholdDef(
        "ads.ctr_bench", "CTR 基准", "广告诊断", 0.02, "ratio", 0.001, 0.30,
        "低于此值判为创意/素材问题，而不是出价问题。不同平台差异极大，务必按平台校准",
    ),
    "ads.min_impressions": ThresholdDef(
        "ads.min_impressions", "最小曝光量", "广告诊断", 1000, "count", 100, 100000,
        "曝光低于此值不做判断，样本不足时任何 ROAS 都没有意义",
    ),
    "ads.min_clicks": ThresholdDef(
        "ads.min_clicks", "最小点击数", "广告诊断", 30, "count", 5, 5000,
        "点击低于此值不判「零单」，避免把偶然当成结论",
    ),
    "ads.bleeding_ratio": ThresholdDef(
        "ads.bleeding_ratio", "失血系数", "广告诊断", 0.5, "index", 0.1, 1.0,
        "ROAS 低于「盈亏线 × 此系数」判为 P0 失血组，需立即降预算",
    ),
    # --- 市场调研 ---
    "market.cr3_high": ThresholdDef(
        "market.cr3_high", "高度集中线（CR3）", "市场调研", 0.6, "ratio", 0.3, 0.95,
        "前三品牌销量占比高于此值，判为高度集中，新店不宜正面硬刚",
    ),
    "market.cr3_mid": ThresholdDef(
        "market.cr3_mid", "中度集中线（CR3）", "市场调研", 0.4, "ratio", 0.2, 0.9,
        "前三品牌销量占比高于此值但未达高度集中线，判为中度集中",
    ),
    "market.opportunity_blue": ThresholdDef(
        "market.opportunity_blue", "蓝海门槛（机会指数）", "市场调研", 1.2, "index", 1.0, 5.0,
        "销量份额 ÷ 商品数份额 高于此值判为供不应求",
    ),
    "market.opportunity_red": ThresholdDef(
        "market.opportunity_red", "红海门槛（机会指数）", "市场调研", 0.8, "index", 0.1, 1.0,
        "机会指数低于此值判为供给过剩",
    ),
    "market.min_band_products": ThresholdDef(
        "market.min_band_products", "价格带最小样本", "市场调研", 3, "count", 2, 30,
        "某价格带商品数少于该值时不打「红海」标签，样本不足不能下结论",
    ),
    "market.min_monthly_sales": ThresholdDef(
        "market.min_monthly_sales", "类目容量门槛", "市场调研", 5000, "count", 100, 1000000,
        "类目月销量低于此值判为容量不足，进入评分扣分",
    ),
    "market.verdict_pass_score": ThresholdDef(
        "market.verdict_pass_score", "可进入分数门槛", "市场调研", 3, "count", 1, 4,
        "进入评分（满分 4）达到此值才建议「可进」",
    ),
    # --- 选品定价 ---
    "strategy.target_margin": ThresholdDef(
        "strategy.target_margin", "目标净利率", "选品定价", 0.20, "ratio", 0.02, 0.6,
        "反算达标最低售价用的目标净利率",
    ),
    "strategy.default_return_rate": ThresholdDef(
        "strategy.default_return_rate", "默认退货率", "选品定价", 0.02, "ratio", 0.0, 0.5,
        "用户没传退货率时用的默认值，直接影响净收入",
    ),
    # --- 评论归因 ---
    "review.good_rate_target": ThresholdDef(
        "review.good_rate_target", "好评率目标", "评论归因", 0.85, "ratio", 0.5, 1.0,
        "低于此值把差评列为需要优先处理的信号",
    ),
    "review.fixable_rate_target": ThresholdDef(
        "review.fixable_rate_target", "可修复率目标", "评论归因", 0.5, "ratio", 0.1, 1.0,
        "差评中可内容修复的比例低于此值，说明问题偏供应链",
    ),
    "review.p0_share": ThresholdDef(
        "review.p0_share", "单类差评升 P0 门槛", "评论归因", 0.3, "ratio", 0.05, 0.9,
        "某一类差评占全部差评比例达到此值，对应动作升为 P0",
    ),
    # --- Listing ---
    "listing.stuffing_limit": ThresholdDef(
        "listing.stuffing_limit", "词频上限（堆砌判定）", "Listing", 3, "count", 2, 10,
        "同一实词出现达到此次数即判为堆砌关键词，校验不通过",
    ),
    # --- 短视频 ---
    "video.chars_per_sec_zh": ThresholdDef(
        "video.chars_per_sec_zh", "中文语速（字/秒）", "短视频", 4.5, "decimal", 2, 8,
        "中文口播语速，用于测算时长是否够念完",
    ),
    "video.syllables_per_sec": ThresholdDef(
        "video.syllables_per_sec", "拉丁语速（音节/秒）", "短视频", 4.0, "decimal", 1.5, 7,
        "英/葡等拉丁语系口播语速",
    ),
    "video.too_thin_ratio": ThresholdDef(
        "video.too_thin_ratio", "空档告警线", "短视频", 0.7, "ratio", 0.3, 1.0,
        "口播所需时长低于分镜总时长 × 此值，提示空档过大",
    ),
}

# 平台级覆盖：这些差异是真实存在的，不是拍脑袋
PLATFORM_OVERRIDES: dict[str, dict[str, float]] = {
    "shopee_br": {"ads.ctr_bench": 0.025},
    # Amazon 的广告 CTR 普遍在 0.4% 上下，用 2% 去卡会把正常广告全判成创意失败
    "amazon": {"ads.ctr_bench": 0.004, "ads.min_impressions": 2000},
    "mercadolivre": {"ads.ctr_bench": 0.018},
    "tiktok_shop": {"ads.ctr_bench": 0.03, "video.chars_per_sec_zh": 5.0},
    "shopify": {"ads.ctr_bench": 0.015},
}

SOURCE_LABEL = {
    "request": "请求参数",
    "override": "界面设置",
    "platform": "平台预设",
    "default": "系统默认",
}


class Config:
    """一次运行内生效的阈值集合，每个值都带来源。"""

    store_id: str = "default"

    def __init__(self, values: dict[str, float], sources: dict[str, str], platform: str = ""):
        self._values = values
        self._sources = sources
        self.platform = platform

    def get(self, key: str) -> float:
        if key not in self._values:
            raise KeyError(f"未定义的阈值: {key}")
        return self._values[key]

    def get_int(self, key: str) -> int:
        return int(self.get(key))

    def source_of(self, key: str) -> str:
        return self._sources.get(key, "default")

    def source_label(self, key: str) -> str:
        return SOURCE_LABEL.get(self.source_of(key), self.source_of(key))

    def describe(self, key: str) -> str:
        return THRESHOLDS[key].fmt(self.get(key))

    def as_list(self) -> list[dict]:
        """给前端用的完整清单。"""
        out = []
        for key, d in THRESHOLDS.items():
            value = self._values.get(key, d.default)
            out.append(
                {
                    "key": key,
                    "label": d.label,
                    "group": d.group,
                    "kind": d.kind,
                    "value": value,
                    "display": d.fmt(value),
                    "default": d.default,
                    "low": d.low,
                    "high": d.high,
                    "desc": d.desc,
                    "source": self.source_of(key),
                    "source_label": self.source_label(key),
                    "changed": abs(value - d.default) > 1e-9,
                }
            )
        return out

    def flat(self) -> dict[str, float]:
        return dict(self._values)


def _coerce(key: str, raw: Any) -> float | None:
    """把外部传入的值转成合法数字，超范围直接丢弃（不用非法值污染判断）。"""
    d = THRESHOLDS.get(key)
    if not d:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < d.low or value > d.high:
        return None
    return value


def load_config(
    platform: str | None = None,
    request_overrides: dict | None = None,
    store_id: str = "default",
) -> Config:
    """按优先级合成一次运行使用的阈值。

    界面覆盖值按店铺隔离：A 店把 CTR 基准调到 5%，不能影响 B 店。
    """
    values = {k: d.default for k, d in THRESHOLDS.items()}
    sources = {k: "default" for k in THRESHOLDS}

    # 平台覆盖
    if platform and platform in PLATFORM_OVERRIDES:
        for key, val in PLATFORM_OVERRIDES[platform].items():
            if key in values:
                values[key] = val
                sources[key] = "platform"

    # 界面保存的覆盖
    try:
        for key, raw in db.get_settings(store_id).items():
            val = _coerce(key, raw)
            if val is not None:
                values[key] = val
                sources[key] = "override"
    except Exception:  # noqa: BLE001  读配置失败不应让运行挂掉
        pass

    # 请求显式传入
    if request_overrides:
        for key, raw in request_overrides.items():
            val = _coerce(key, raw)
            if val is not None:
                values[key] = val
                sources[key] = "request"

    cfg = Config(values, sources, platform or "")
    cfg.store_id = store_id
    return cfg


def update_overrides(patch: dict, store_id: str = "default") -> dict:
    """保存界面上的修改。空值表示恢复默认（删除该条覆盖）。"""
    accepted: dict[str, float] = {}
    rejected: list[str] = []
    resetting: list[str] = []

    for key, raw in (patch or {}).items():
        if key not in THRESHOLDS:
            rejected.append(key)
            continue
        if raw is None or raw == "":
            resetting.append(key)
            continue
        val = _coerce(key, raw)
        if val is None:
            rejected.append(key)
        else:
            accepted[key] = val

    for key in resetting:
        db.delete_setting(key, store_id)
    if accepted:
        db.save_settings(accepted, store_id)

    return {"updated": list(accepted), "reset": resetting, "rejected": rejected}


def reset_all(store_id: str = "default") -> None:
    for key in THRESHOLDS:
        db.delete_setting(key, store_id)
