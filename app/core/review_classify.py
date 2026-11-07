"""评论自动归因 —— 把「人工一条条打标」变成「机器分好、人只复核」。

为什么规则优先、LLM 兜底，而不是全交给模型：

1. 差评的表达高度模板化（"没电""坏了""晚了""缺配件"），关键词命中率很高；
2. 规则是确定性的 —— 同样的评论永远归到同一类，可以回归测试；
3. 跨境评论是葡语/英语/中文混杂，规则能覆盖高频模式，模型处理长尾。

效率差别很大：300 条评论走规则是毫秒级且免费，全走模型要 15 次请求。
"""

from __future__ import annotations

import json
import re

from app.core.llm import try_generate

# 八类归因的关键词库（中文 / 英文 / 葡语）
# 跨境场景必须三语覆盖：巴西站评论是葡语，亚马逊是英语，内部复盘是中文
RULES: dict[str, list[str]] = {
    "破损/故障/退货": [
        "坏", "破损", "碎了", "故障", "退货", "不能用", "开不了机", "死机", "裂了", "漏电",
        "broken", "defective", "damaged", "stopped working", "not working", "refund", "return", "cracked", "dead",
        "quebrado", "quebrada", "danificado", "não funciona", "nao funciona", "parou de", "defeito", "estragou",
        "amassad", "arranhad", "rachad", "trincad", "roda", "não liga", "nao liga",
    ],
    "配件缺失": [
        "缺", "少了", "没有配", "漏发", "没给", "没带", "没送",
        "missing", "no charger", "didn't come with", "did not come with", "no manual", "without",
        "faltou", "veio sem", "não veio", "nao veio", "sem o", "faltando",
    ],
    "参数不符合预期": [
        "续航", "电池", "不耐用", "功率", "没到", "虚标", "太小", "重量", "速度不够", "跑不到",
        "battery", "lasts", "autonomy", "power", "smaller", "heavier", "not as powerful", "overrated", "only get",
        "bateria", "autonomia", "potência", "potencia", "menor", "não dura", "nao dura", "mais pesado",
        "durou menos", "menos que", "abaixo do", "não é 500", "nao e 500",
    ],
    "描述不符": [
        "不一样", "色差", "和图", "和描述", "实物", "差别", "不是这个颜色",
        "not as described", "different from", "color is", "looks different", "not the same",
        "diferente", "não é como", "nao e como", "cor diferente", "não corresponde",
    ],
    "误解型": [
        "以为", "没想到", "看错", "不知道要", "原来不", "误会", "以为是",
        "thought it", "assumed", "didn't know", "did not know", "expected it to", "i thought",
        "achei que", "pensei que", "não sabia", "nao sabia", "imaginei",
    ],
    "使用困难": [
        "难装", "不会用", "看不懂", "说明书", "难折", "不好装", "装不上", "不会装", "太复杂",
        "hard to", "difficult", "instructions", "can't figure", "cannot figure", "complicated", "confusing",
        "difícil", "dificil", "não consegui", "nao consegui", "manual ruim", "complicado",
    ],
    "发货慢": [
        "慢", "晚到", "等了", "物流", "迟迟", "还没到",
        "slow", "late", "delay", "still waiting", "never arrived", "shipping took",
        "demorou", "atrasou", "demora", "ainda não chegou", "nao chegou",
    ],
    "价格偏高": [
        "贵", "不值", "性价比低", "太贵", "不值这个价",
        "expensive", "overpriced", "not worth", "too pricey",
        "caro", "não vale", "nao vale", "muito caro",
    ],
}

# 强信号：一旦出现就锁定类别，不再看其他关键词。
#
# 解决的是「语义反转」问题：
#   "A bateria durou menos do que eu esperava" —— 字面命中「电池」，
#   但这句在说「我的预期错了」，属于误解型，不是产品参数有问题。
# 这两类的补救动作完全相反：一个改详情页，一个改产品。
STRONG_SIGNALS: dict[str, list[str]] = {
    "误解型": [
        "esperava", "esperei", "esperando", "imaginei", "imaginava", "supus",
        "achei que", "pensei que", "não sabia que", "nao sabia que", "deveria vir",
        "以为", "没想到", "原以为", "看错了", "不知道要",
        "thought it", "i assumed", "expected it to", "didn't realize", "did not realize",
    ],
    "破损/故障/退货": [
        "amassad", "arranhad", "rachad", "trincad", "veio quebrad",
        "parou de funcionar", "não liga", "nao liga",
        "stopped working", "won't turn on", "wont turn on",
        "开不了机", "收到就是坏的", "用两天就坏",
    ],
    "配件缺失": [
        "faltou", "veio sem", "não veio com", "nao veio com", "sem o carregador",
        "missing", "didn't come with", "did not come with",
        "漏发", "少发了", "没收到配件",
    ],
}

# 并列命中时的优先级：越靠前越影响退货与评分
CATEGORY_PRIORITY = [
    "破损/故障/退货",
    "配件缺失",
    "参数不符合预期",
    "描述不符",
    "误解型",
    "使用困难",
    "发货慢",
    "价格偏高",
]

PENDING = "待人工确认"

LLM_BATCH = 20


def _normalize(text: str) -> str:
    return str(text or "").lower()


def classify_by_rules(text: str) -> tuple[str, int, str]:
    """返回 (类别, 命中关键词数, 命中的词)。都没命中返回 ('', 0, '')。

    两段式：
    1. 强信号（元表达）一旦命中直接锁定 —— 它表达的是「用户预期错了」，
       比「提到了什么参数」更能说明归因本质；
    2. 否则按命中关键词数排序，并列时按 CATEGORY_PRIORITY。
    """
    norm = _normalize(text)
    if not norm.strip():
        return "", 0, ""

    for cat in CATEGORY_PRIORITY:
        for sig in STRONG_SIGNALS.get(cat, []):
            if sig in norm:
                return cat, 3, [sig]

    best_cat, best_hits, best_words = "", 0, []
    for cat in CATEGORY_PRIORITY:
        words = [w for w in RULES.get(cat, []) if w in norm]
        if len(words) > best_hits:
            best_cat, best_hits, best_words = cat, len(words), words

    return best_cat, best_hits, best_words


def _rule_confidence(hits: int) -> tuple[str, float]:
    if hits >= 3:
        return "high", 0.9
    if hits == 2:
        return "medium", 0.75
    return "low", 0.6


def _llm_classify(texts: list[str]) -> list[str]:
    """批量让模型分类。返回与输入等长的类别列表，失败的位填空串。"""
    if not texts:
        return []

    categories = "、".join(CATEGORY_PRIORITY)
    numbered = "\n".join(f"{i + 1}. {t[:200]}" for i, t in enumerate(texts))

    system = (
        "你是电商差评归因助手。只做分类，不解释、不评价、不输出额外文字。"
        f"可选类别只有这八个：{categories}。"
    )
    user = f"""把下面每条评论归到最贴切的一类。判断依据是「用户真正不满的是什么」，不是字面词。

{numbered}

只输出 JSON 数组，长度必须等于 {len(texts)}，每项是类别名本身：
["类别1", "类别2", ...]"""

    raw = try_generate(system, user, max_tokens=800)
    if not raw:
        return [""] * len(texts)

    data = None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        match = re.search(r"\[[\s\S]*\]", raw)
        if match:
            try:
                data = json.loads(match.group(0))
            except (json.JSONDecodeError, TypeError):
                data = None

    if not isinstance(data, list):
        return [""] * len(texts)

    out: list[str] = []
    for i in range(len(texts)):
        value = str(data[i]).strip() if i < len(data) else ""
        # 模型可能输出「1. 破损/故障/退货」这类，做一次清洗
        value = re.sub(r"^\s*\d+[.、)]\s*", "", value)
        out.append(value if value in CATEGORY_PRIORITY else "")
    return out


def classify_batch(reviews: list[dict], use_llm: bool = True) -> tuple[list[dict], dict]:
    """给没有 category 的评论自动归因。

    已有 category 的评论原样保留（尊重人工标注）。
    返回 (补充后的评论列表, 统计)。
    """
    stats = {
        "total": len(reviews),
        "needing": 0,     # 真正需要归因的（差评）
        "not_needed": 0,  # 好评不需要归因
        "manual": 0,      # 原本就有人工标注
        "rule_high": 0,
        "rule_medium": 0,
        "rule_low": 0,
        "llm": 0,
        "pending": 0,     # 实在分不出来的
    }

    pending_idx: list[int] = []
    out: list[dict] = []

    for i, review in enumerate(reviews):
        item = dict(review)

        # 只对差评做归因。好评没有「不满的原因」，给它分类纯属制造噪声，
        # 而且会把覆盖率的分母撑大，让结果看起来很差（这是真踩过的坑）。
        try:
            rating = float(item.get("rating") or 0)
        except (TypeError, ValueError):
            rating = 0
        if rating >= 3:
            item["classify_method"] = "not_needed"
            stats["not_needed"] += 1
            out.append(item)
            continue

        stats["needing"] += 1
        existing = str(item.get("category") or "").strip()
        if existing and existing not in (PENDING, "未标注"):
            stats["manual"] += 1
            item["classify_method"] = "manual"
            out.append(item)
            continue

        text = str(item.get("text") or "")
        cat, hits, words = classify_by_rules(text)
        if cat:
            level, conf = _rule_confidence(hits)
            item["category"] = cat
            item["classify_method"] = "rule"
            item["classify_confidence"] = conf
            item["classify_evidence"] = "、".join(words[:3])
            stats[f"rule_{level}"] += 1
        else:
            item["category"] = PENDING
            item["classify_method"] = "pending"
            item["classify_confidence"] = 0.0
            pending_idx.append(i)
        out.append(item)

    # 规则没命中的交给模型（批量，避免几百次请求）
    if use_llm and pending_idx:
        for start in range(0, len(pending_idx), LLM_BATCH):
            chunk = pending_idx[start : start + LLM_BATCH]
            texts = [str(out[i].get("text") or "") for i in chunk]
            labels = _llm_classify(texts)
            for idx, label in zip(chunk, labels):
                if label:
                    out[idx]["category"] = label
                    out[idx]["classify_method"] = "llm"
                    out[idx]["classify_confidence"] = 0.7
                    stats["llm"] += 1
                else:
                    stats["pending"] += 1

    # 兜底没进模型的（use_llm=False 时）
    if not use_llm:
        stats["pending"] += len(pending_idx)

    stats["auto"] = stats["rule_high"] + stats["rule_medium"] + stats["rule_low"] + stats["llm"]
    # 覆盖率的分母是「需要归因的评论数」，不是全部评论
    stats["coverage"] = round(stats["auto"] / stats["needing"], 4) if stats["needing"] else 0.0
    return out, stats
