"""03 Listing 文案 Agent —— 标题是唯一能被搜索的资产，所以它必须过机器校验。

两件事分开做：
- 生成：按「核心词 → 属性 → 规格 → 场景」的结构拼，一次给三个变体
- 校验：字符数、前置权重区、关键词覆盖、堆砌、营销噪音，全部由脚本判定
生成可以靠模型，能不能发必须靠脚本。
"""

from __future__ import annotations

from app.agents.base import StageAgent, action, fmt_pct, insight
from app.core import metrics as M
from app.core.contracts import StageResult
from app.core.platforms import get_platform

# 详情页七段结构：少一段就少一个转化理由
DETAIL_SECTIONS = [
    ("首屏利益点", "3 秒内说清「买它解决什么问题」，不写品牌故事"),
    ("使用场景", "2-3 个具体场景图配文，让人对号入座"),
    ("核心卖点拆解", "每条卖点 = 参数 + 对用户意味着什么"),
    ("完整参数表", "所有数字必须能溯源到实物规格，禁止虚标"),
    ("与同类差异", "只对比可验证项（配置/尺寸/材质），不贬低竞品"),
    ("注意事项", "把差评里「误解型」问题前置说明，这是最省钱的售后"),
    ("包装清单与售后", "列全配件，避免「配件缺失」类差评"),
]


class ListingAgent(StageAgent):
    key = "listing"

    def _run(self, payload: dict) -> StageResult:
        platform = get_platform(payload.get("platform"))
        name = str(payload["product_name"]).strip()
        points = [str(p).strip() for p in payload["selling_points"] if str(p).strip()]
        raw_keywords = payload["keywords"]

        keywords, weighted = self._normalize_keywords(raw_keywords)
        specs = payload.get("specs") or {}
        audience = payload.get("target_audience") or ""

        variants = self._compose_titles(
            name, points, keywords, weighted, specs, audience, platform.title_max
        )
        titles = []
        for variant in variants:
            lint = M.title_lint(
                title=variant["text"],
                title_max=platform.title_max,
                front_weight=platform.title_front_weight,
                keywords=keywords,
            )
            titles.append({**variant, "lint": lint})

        best = max(titles, key=lambda t: (t["lint"]["passed"], t["lint"]["keyword_coverage"]))

        insights = []
        actions = []
        evidence = ["metrics.title_lint", f"platform:{platform.key}.title_max={platform.title_max}"]

        # 语言守卫：中文卖点直接进外语标题是最常见的低级错误
        if _has_cjk(name) or any(_has_cjk(p) for p in points):
            insights.append(
                insight(
                    f"检测到产品名/卖点含中文，而 {platform.name} 面向 {platform.language} 买家："
                    f"标题已自动剔除中文内容，避免出现「一半葡语一半中文」的链接。"
                    f"卖点需先翻译成目标语言（当地表达，不是直译）才能进标题。",
                    "warn",
                    "listing.language_guard",
                )
            )

        if best["lint"]["over_limit"]:
            insights.append(
                insight(
                    f"最佳变体仍超限 {best['lint']['chars'] - platform.title_max} 字符："
                    f"{platform.name} 上限 {platform.title_max}，超限会被截断或直接拒收。",
                    "risk",
                    "title_lint.over_limit",
                )
            )
        else:
            insights.append(
                insight(
                    f"标题字符数 {best['lint']['chars']} / 上限 {platform.title_max}，"
                    f"前 {platform.title_front_weight} 字符权重区"
                    f"{'已含核心词' if best['lint']['front_has_keyword'] else '未含核心词（须调整词序）'}。",
                    "good" if best["lint"]["front_has_keyword"] else "warn",
                    "title_lint.front_weight_zone",
                )
            )

        if best["lint"]["keyword_missing"]:
            insights.append(
                insight(
                    f"还有 {len(best['lint']['keyword_missing'])} 个关键词没进标题："
                    f"{'、'.join(best['lint']['keyword_missing'][:5])}。"
                    f"关键词覆盖率 {fmt_pct(best['lint']['keyword_coverage'])}，"
                    f"缺失词应下沉到详情页和后台搜索词，不要硬塞。",
                    "warn",
                    "title_lint.keyword_missing",
                )
            )

        if points:
            insights.append(
                insight(
                    f"主卖点必须收敛成唯一一句：「{points[0]}」。"
                    f"标题只服务这句，其他卖点放详情页第 3 段，否则主图和视频会各讲一件事。",
                    "info",
                    "selling_points[0]",
                )
            )

        headline = (
            f"{platform.name} · 生成 {len(titles)} 个标题变体，"
            f"最佳 {best['lint']['chars']} 字符 / 覆盖 {fmt_pct(best['lint']['keyword_coverage'])} 关键词"
            + ("，校验通过" if best["lint"]["passed"] else "，校验未通过")
        )

        actions.append(
            action(
                "P0",
                "标题",
                f"采用变体「{best['variant']}」：{best['text']}",
                f"字符与关键词覆盖综合最优（{best['lint']['chars']}/{platform.title_max}）",
                "上架 7 天后看该链接曝光量是否高于同批其他变体",
            )
        )
        if best["lint"]["keyword_missing"]:
            actions.append(
                action(
                    "P1",
                    "后台搜索词",
                    "把未进标题的关键词填进后台 search terms 字段",
                    "标题塞不下不代表不能覆盖，后台字段是第二个免费流量入口",
                    "两周后看这些词的搜索曝光是否出现",
                )
            )
        actions.append(
            action(
                "P1",
                "详情页",
                "按七段结构重写，第 6 段「注意事项」必须覆盖差评中的误解型问题",
                "误解型差评改文案就能消掉，是成本最低的评分修复手段",
                "改版后 30 天内「误解型」差评占比是否下降",
                "中",
            )
        )

        return StageResult(
            stage=self.key,
            title=self.title,
            status="ok",
            headline=headline,
            metrics={
                "titles": titles,
                "best": best,
                "detail_sections": [{"title": t, "guide": g} for t, g in DETAIL_SECTIONS],
                "keywords": keywords,
                "platform": {
                    "key": platform.key,
                    "name": platform.name,
                    "title_max": platform.title_max,
                    "language": platform.language,
                },
            },
            insights=insights,
            actions=actions,
            evidence=evidence,
            next_stage="image",
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_keywords(raw) -> tuple[list[str], dict[str, float]]:
        """兼容两种入参：字符串列表，或带搜索量的对象列表。"""
        keywords: list[str] = []
        weighted: dict[str, float] = {}
        for item in raw:
            if isinstance(item, dict):
                term = str(item.get("term") or item.get("word") or "").strip()
                if term:
                    keywords.append(term)
                    weighted[term] = float(item.get("volume") or 0)
            else:
                term = str(item).strip()
                if term:
                    keywords.append(term)
                    weighted[term] = 0.0
        return keywords, weighted

    @staticmethod
    def _compose_titles(
        name: str,
        points: list[str],
        keywords: list[str],
        weighted: dict[str, float],
        specs: dict,
        audience: str,
        title_max: int = 120,
    ) -> list[dict]:
        """三个变体：搜索导向 / 转化导向 / 人群导向。

        为什么一定要三个：单链接无法同时验证「哪个词序更抓搜索」和
        「哪个卖点更抓转化」，多链接小流量测试才能拿到答案。

        两个硬约束：
        1. 不超过平台字符上限（Shopee 120 字符只用 43 是白白浪费搜索位）
        2. 中文不进外语标题 —— 混语言链接会被平台判为低质
        """
        ordered = sorted(keywords, key=lambda k: weighted.get(k, 0), reverse=True) or [name]
        core = ordered[0] if ordered else name
        pool = [k for k in ordered[1:] if k and k != core]

        spec_bits = []
        for key in ("capacity", "power", "size", "material", "color"):
            if key in specs and specs[key]:
                spec_bits.append(str(specs[key]))

        # 中文内容不能进目标语言标题，先过滤掉
        safe_points = [p for p in points if not _has_cjk(p)]
        safe_audience = audience if audience and not _has_cjk(audience) else ""
        point = safe_points[0] if safe_points else ""

        def fill(base: str, extra: list[str], limit: int) -> str:
            """在字符上限内贪心补词，并自我约束：任何实词不得出现 3 次以上。

            这条约束来自校验器的反馈 —— 生成器堆词，校验器判不通过，
            那就把校验器的规则前置到生成器里。生成与校验共用一套标准。
            """
            text = base.strip()

            def counts(s: str) -> dict[str, int]:
                out: dict[str, int] = {}
                for w in s.replace("-", " ").replace("|", " ").split():
                    if len(w) > 2:
                        key = w.lower()
                        out[key] = out.get(key, 0) + 1
                return out

            for term in extra:
                if not term:
                    continue
                candidate = f"{text} {term}".strip()
                if len(candidate) > limit:
                    continue
                merged = counts(candidate)
                if any(v >= 3 for v in merged.values()):
                    continue  # 会触发堆砌校验，跳过
                text = candidate
            return text

        spec_phrase = " ".join(spec_bits[:2])
        attr_phrase = " ".join(pool[:3])

        v1 = fill(" ".join(x for x in [core, attr_phrase] if x), [spec_phrase] + pool, title_max)
        v2 = fill(" ".join(x for x in [core, point, spec_phrase] if x), pool, title_max)
        v3 = fill(" ".join(x for x in [core, safe_audience, attr_phrase] if x), [spec_phrase] + pool, title_max)

        seen: set[str] = set()
        out: list[dict] = []
        for label, text in [
            ("搜索导向", v1),
            ("转化导向", v2),
            ("人群导向", v3),
        ]:
            text = " ".join(text.split())
            if text and text not in seen:
                seen.add(text)
                out.append({"variant": label, "text": text})
        return out


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text))
