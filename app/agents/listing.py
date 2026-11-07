"""03 Listing 文案 Agent —— 标题是唯一能被搜索的资产，所以它必须过机器校验。

两件事分开做：
- 生成：按「核心词 → 属性 → 规格 → 场景」的结构拼，一次给三个变体
- 校验：字符数、前置权重区、关键词覆盖、堆砌、营销噪音，全部由脚本判定
生成可以靠模型，能不能发必须靠脚本。
"""

from __future__ import annotations

import json
import re

from app.agents.base import StageAgent, action, fmt_pct, insight
from app.core import config, metrics as M
from app.core.contracts import StageResult
from app.core.llm import provider_info, try_generate
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
        cfg = config.load_config(payload.get("platform"), payload.get("thresholds"))
        stuffing_limit = cfg.get_int("listing.stuffing_limit")
        name = str(payload["product_name"]).strip()
        points = [str(p).strip() for p in payload["selling_points"] if str(p).strip()]
        raw_keywords = payload["keywords"]

        keywords, weighted = self._normalize_keywords(raw_keywords)
        specs = payload.get("specs") or {}
        audience = payload.get("target_audience") or ""

        variants = self._compose_titles(
            name, points, keywords, weighted, specs, audience,
            platform.title_max, stuffing_limit
        )

        # 先问模型要三版，再让脚本逐条验；验不过的直接丢，规则版兜底。
        titles = self._llm_titles(name, points, keywords, specs, audience, platform)
        llm_used = bool(titles)
        for variant in variants:
            lint = M.title_lint(
                title=variant["text"],
                title_max=platform.title_max,
                front_weight=platform.title_front_weight,
                keywords=keywords,
                stuffing_limit=stuffing_limit,
            )
            titles.append({**variant, "lint": lint, "source": "rule"})

        best = max(titles, key=lambda t: (t["lint"]["passed"], t["lint"]["keyword_coverage"]))

        insights = []
        actions = []
        evidence = ["metrics.title_lint", f"platform:{platform.key}.title_max={platform.title_max}"]

        if llm_used:
            insights.append(
                insight(
                    f"标题由 {provider_info()['label']}（{provider_info()['model']}）生成后，"
                    f"逐条通过字符数 / 关键词覆盖 / 堆砌三项脚本校验，"
                    f"{sum(1 for t in titles if t['source'] == 'llm')} 条合规。"
                    f"模型只负责写，能不能发由脚本判定。",
                    "good",
                    "listing.llm",
                )
            )

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
                "llm": {"used": llm_used, **provider_info()},
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
    def _llm_titles(
        self,
        name: str,
        points: list[str],
        keywords: list[str],
        specs: dict,
        audience: str,
        platform,
    ) -> list[dict]:
        """让模型写标题，但**只收通过脚本校验的**。

        这是「生成与校验共用一套标准」的落地：
        模型再自信，字符超限、关键词全丢、同一个词出现四次，一样不收。
        """
        if not keywords:
            return []

        point_text = "；".join(points[:3]) if points else "（无）"
        spec_text = "，".join(f"{k}={v}" for k, v in list(specs.items())[:4]) or "（无）"
        kw_text = "、".join(keywords[:6])

        system = (
            f"你是 {platform.name} 的资深 listing 文案，只为 {platform.language} 买家写标题。"
            f"你写的标题必须像本地人写的，不是翻译腔。"
        )
        user = f"""为下面这个商品写 3 条标题备选。

产品：{name}
核心卖点：{point_text}
规格：{spec_text}
必须覆盖的关键词（按搜索量降序）：{kw_text}
目标人群：{audience or "（未指定）"}

硬约束：
1. 只用 {platform.language}，禁止出现中文，禁止中英混写
2. 每条不超过 {platform.title_max} 个字符（含空格），这是平台的硬限制
3. 最重要的大词放在最前面，前 {platform.title_front_weight} 个字符权重最高
4. 同一个实词最多出现 2 次，禁止堆砌同义词
5. 三条要有明显差异：一条搜索词优先、一条卖点优先、一条场景/人群优先

只输出 JSON 数组，三个字符串，不要任何解释：
["标题一", "标题二", "标题三"]"""

        raw = try_generate(system, user, max_tokens=500)
        if not raw:
            return []

        out: list[dict] = []
        for idx, text in enumerate(self._parse_candidates(raw)[:3]):
            text = " ".join(text.split())
            if _has_cjk(text):
                continue  # 模型混了中文，直接丢
            lint = M.title_lint(
                title=text,
                title_max=platform.title_max,
                front_weight=platform.title_front_weight,
                keywords=keywords,
                stuffing_limit=stuffing_limit,
            )
            if not lint["passed"]:
                continue  # 超限或堆砌，不收
            out.append(
                {
                    "variant": f"大模型方案 {idx + 1}",
                    "text": text,
                    "lint": lint,
                    "source": "llm",
                }
            )
        return out

    @staticmethod
    def _parse_candidates(raw: str) -> list[str]:
        """尽力从模型输出里捞出标题：JSON 数组 → 数组片段 → 逐行。"""
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except (json.JSONDecodeError, TypeError):
            pass

        match = re.search(r"\[[\s\S]*\]", raw)
        if match:
            try:
                data = json.loads(match.group(0))
                if isinstance(data, list):
                    return [str(x).strip() for x in data if str(x).strip()]
            except (json.JSONDecodeError, TypeError):
                pass

        lines: list[str] = []
        for line in raw.splitlines():
            cleaned = re.sub(r"^\s*[-*\d.)\]]+\s*", "", line).strip().strip('",')
            if len(cleaned) > 10 and not cleaned.startswith("```"):
                lines.append(cleaned)
        return lines

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
        stuffing_limit: int = 3,
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
                if any(v >= stuffing_limit for v in merged.values()):
                    continue  # 会触发堆砌校验，跳过
                text = candidate
            return text

        spec_phrase = " ".join(spec_bits[:2])

        # 注意：三个变体都以「core」为唯一起点，后续每个词都经过 fill 的堆砌检查。
        # 早期版本用 " ".join(pool[:3]) 先拼好基础串，导致 core 里的词被重复带入，
        # 生成出 patinete ×4 的标题，而堆砌检查发生在之后，抓不到。
        v1 = fill(core, pool + ([spec_phrase] if spec_phrase else []), title_max)
        v2 = fill(" ".join(x for x in [core, point, spec_phrase] if x), pool, title_max)
        v3 = fill(" ".join(x for x in [core, safe_audience] if x), pool + ([spec_phrase] if spec_phrase else []), title_max)

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
