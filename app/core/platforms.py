"""平台规则卡 —— 所有硬指标的唯一出处。

原则：平台规则会变，所以全部集中在这里，任何 Agent 都不许内联写死字符数、
费率或图片规格。改规则只改这一个文件。

数值来源：各平台卖家后台公开规范（编写时快照），发布前需人工复核。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PlatformSpec:
    """一个平台的运营硬约束。"""

    key: str
    name: str
    title_max: int
    # 标题前 N 个字符权重最高（移动端截断）
    title_front_weight: int
    # 佣金（含平台服务费）综合费率
    commission_rate: float
    # 综合有效税率（跨境折算，非真实税制明细）
    tax_rate: float
    # 主图硬要求
    image_min_px: int
    image_bg: str
    image_max_count: int
    # 输出语言
    language: str
    # 日期列默认顺序：报表里 01/11 这类写法在数据内找不到证据时的兜底。
    # 巴西/欧洲是 日/月，美国是 月/日 —— 猜错会把趋势图整体错位。
    date_order: str = "dmy"
    notes: list[str] = field(default_factory=list)


PLATFORMS: dict[str, PlatformSpec] = {
    "shopee_br": PlatformSpec(
        key="shopee_br",
        name="Shopee 巴西站",
        title_max=120,
        title_front_weight=30,
        commission_rate=0.14,
        tax_rate=0.08,
        image_min_px=1000,
        image_bg="white",
        image_max_count=9,
        language="pt-BR",
        notes=[
            "标题前 30 字符权重最高，核心词必须前置",
            "混售/多品同图会被判违规，主图只允许单品",
            "已产生花费的广告组当天不可下调预算，只能等次日归零",
        ],
    ),
    "mercadolivre": PlatformSpec(
        key="mercadolivre",
        name="Mercado Livre",
        title_max=60,
        title_front_weight=60,
        commission_rate=0.16,
        tax_rate=0.06,
        image_min_px=1200,
        image_bg="clean",
        image_max_count=10,
        language="pt-BR",
        notes=[
            "标题上限仅 60 字符，词序即优先级，禁止堆砌同义词",
            "主图主体需占画面 85%-95%，留白过多会降权",
        ],
    ),
    "amazon": PlatformSpec(
        key="amazon",
        name="Amazon",
        title_max=200,
        title_front_weight=80,
        commission_rate=0.15,
        tax_rate=0.0,
        image_min_px=1000,
        image_bg="pure_white",
        image_max_count=9,
        language="en",
        date_order="mdy",  # Amazon 后台报表默认 月/日/年
        notes=[
            "标题建议 80 字符内（移动端可读），上限 200 仅为硬限",
            "主图必须纯白底 RGB(255,255,255)，主体占比 ≥85%",
        ],
    ),
    "tiktok_shop": PlatformSpec(
        key="tiktok_shop",
        name="TikTok Shop",
        title_max=255,
        title_front_weight=40,
        commission_rate=0.05,
        tax_rate=0.0,
        image_min_px=800,
        image_bg="clean",
        image_max_count=9,
        language="en",
        notes=["竖版 9:16 优先，视频素材权重高于图文"],
    ),
    "shopify": PlatformSpec(
        key="shopify",
        name="Shopify 独立站",
        title_max=70,
        title_front_weight=70,
        commission_rate=0.029,
        tax_rate=0.0,
        image_min_px=800,
        image_bg="clean",
        image_max_count=250,
        language="en",
        notes=["无硬性标题上限，但 SERP 约 60-70 字符后截断"],
    ),
}

DEFAULT_PLATFORM = "shopee_br"


def get_platform(key: str | None) -> PlatformSpec:
    """按 key 取平台规则，未知平台回落到默认并保留可追溯性。"""
    if key and key in PLATFORMS:
        return PLATFORMS[key]
    return PLATFORMS[DEFAULT_PLATFORM]


def platform_list() -> list[dict]:
    """给前端用的平台清单。"""
    return [
        {
            "key": p.key,
            "name": p.name,
            "title_max": p.title_max,
            "commission_rate": p.commission_rate,
            "language": p.language,
        }
        for p in PLATFORMS.values()
    ]
