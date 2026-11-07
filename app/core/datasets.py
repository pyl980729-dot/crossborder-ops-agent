"""数据接入 —— 把卖家导出的 CSV 变成系统能吃的结构。

真实痛点不是解析，是**列名对齐**：
同一个「曝光量」，Shopee 后台叫「曝光量」，Amazon 报表叫 Impressions，
ERP 导出叫「展示次数」，还有带单位后缀的「花费(BRL)」。

所以这里干三件事：
1. 猜编码（中文 Excel 导出常是 GBK，不是 UTF-8）
2. 列名模糊匹配（去空格、去括号、大小写不敏感、中英别名）
3. 缺列就说缺哪列、多了哪些列被忽略 —— 不静默丢弃
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

MAX_ROWS = 50_000

# 字段 → 可能出现的列名（全部小写比较）
COLUMN_ALIASES: dict[str, dict[str, list[str]]] = {
    "products": {
        "sku": ["sku", "编码", "商品id", "item_id", "id"],
        "title": ["title", "标题", "商品名", "product_name", "商品标题"],
        "brand": ["brand", "品牌"],
        "price": ["price", "售价", "价格", "单价", "price_brl"],
        "monthly_sales": ["monthly_sales", "月销量", "销量", "月销", "sales", "销量(件)"],
        "rating": ["rating", "评分", "星级", "star", "平均星级"],
        "review_count": ["review_count", "评论数", "评价数"],
        "config": ["config", "配置", "规格", "variant"],
        "is_new_seller": ["is_new_seller", "新卖家", "new_seller"],
        "seller": ["seller", "卖家", "店铺"],
    },
    "ads": {
        "date": ["date", "日期", "统计日期", "日期(day)", "day", "data", "report_date", "投放日期"],
        "campaign": ["campaign", "广告组", "广告名称", "campaign_name", "广告活动", "计划名称"],
        "impressions": ["impressions", "曝光", "曝光量", "展示", "展示次数", "曝光数"],
        "clicks": ["clicks", "点击", "点击量", "点击数"],
        "cost": ["cost", "花费", "消耗", "spend", "广告花费", "花费(brl)"],
        "orders": ["orders", "订单", "订单数", "转化数", "成交数", "订单量"],
        "revenue": ["revenue", "销售额", "gmv", "收入", "转化金额", "成交金额"],
    },
    "reviews": {
        "date": ["date", "日期", "评论日期", "评价日期", "评论时间", "评价时间",
                 "review_date", "created_at", "data"],
        "sku": ["sku", "商品id", "编码"],
        "rating": ["rating", "评分", "星级", "star", "分数"],
        "text": ["text", "评论", "内容", "review", "评价内容", "评论内容"],
        "category": ["category", "归因", "分类", "问题类型"],
        "has_image": ["has_image", "带图", "有图", "含图"],
    },
}

# 这些字段是计数，不该存成 48200.0
INT_FIELDS = {
    "impressions", "clicks", "orders", "is_new_seller", "has_image",
    "review_count", "monthly_sales",
}

NUMERIC_FIELDS: dict[str, set[str]] = {
    "products": {"price", "monthly_sales", "rating", "review_count", "is_new_seller"},
    "ads": {"impressions", "clicks", "cost", "orders", "revenue"},
    "reviews": {"rating", "has_image"},
}

REQUIRED: dict[str, list[str]] = {
    "products": ["price", "monthly_sales"],
    "ads": ["campaign", "impressions", "clicks", "cost", "orders", "revenue"],
    "reviews": ["rating"],
}

FIELD_LABELS = {
    "sku": "SKU", "title": "标题", "brand": "品牌", "price": "售价",
    "monthly_sales": "月销量", "rating": "评分", "review_count": "评论数",
    "config": "配置", "is_new_seller": "新卖家", "seller": "卖家",
    "campaign": "广告组", "impressions": "曝光", "clicks": "点击",
    "cost": "花费", "orders": "订单", "revenue": "销售额",
    "text": "评论内容", "category": "归因", "has_image": "带图", "date": "日期",
}


class DatasetError(ValueError):
    """CSV 无法被接受（编码、缺列、空文件）。"""


def _norm(name: str) -> str:
    """列名归一化：去空白、去括号内容、去标点、转小写。"""
    s = str(name or "").strip().lower()
    s = re.sub(r"[（(].*?[)）]", "", s)  # 花费(BRL) → 花费
    s = re.sub(r"[\s_\-/]+", "", s)
    return s


def _decode(raw: bytes) -> str:
    """猜编码。顺序很重要：UTF-8 优先，再退 GBK，最后兜底。"""
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise DatasetError("无法识别文件编码，请另存为 UTF-8 或 GBK 后重试")


def _map_columns(headers: list[str], kind: str) -> tuple[dict[str, str], list[str], list[str]]:
    """返回 (字段 → 原始列名, 未识别的列, 缺失的必填字段)。"""
    aliases = COLUMN_ALIASES[kind]
    normalized = {_norm(h): h for h in headers}

    mapping: dict[str, str] = {}
    used: set[str] = set()

    # 先精确匹配，再做包含匹配 —— 避免「曝光」把「曝光转化率」也吃掉
    for field, names in aliases.items():
        for alias in names:
            key = _norm(alias)
            if key in normalized:
                mapping[field] = normalized[key]
                used.add(normalized[key])
                break

    for field, names in aliases.items():
        if field in mapping:
            continue
        for norm_col, orig_col in normalized.items():
            if orig_col in used:
                continue
            if any(_norm(a) in norm_col for a in names):
                mapping[field] = orig_col
                used.add(orig_col)
                break

    ignored = [h for h in headers if h not in used]
    missing = [f for f in REQUIRED[kind] if f not in mapping]
    return mapping, ignored, missing


def _to_number(value: Any) -> float | None:
    """把「R$ 1.234,56」「1,234.56」「420,50」这类脏数字转成 float。

    难点是小数分隔符既可能是点也可能是逗号：
      1.234,56  → 欧式，逗号是小数点
      1,234.56  → 美式，点是小数点
      420,50    → 只有逗号，两位小数
      1,234     → 只有逗号，千分位
    判定规则：**两个符号都存在时，后出现的是小数点；
    只有一个逗号且后面恰好 1-2 位时，当作小数点；否则当千分位。**
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)

    s = str(value).strip()
    s = re.sub(r"[R$\s%]", "", s)
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s or s in ("-", ".", ","):
        return None

    has_comma, has_dot = "," in s, "." in s
    if has_comma and has_dot:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_comma:
        tail = s.split(",")[-1]
        s = s.replace(",", ".") if 1 <= len(tail) <= 2 else s.replace(",", "")

    try:
        return float(s)
    except ValueError:
        return None


def _normalize_dates(
    rows: list[dict],
    mapping: dict[str, str],
    warnings: list[str],
    order_hint: str | None,
) -> dict | None:
    """把日期列统一成 ISO 字符串，并记录「用了什么假设」。

    三件事必须摊开说清楚，否则用户拿到错趋势还以为是真的：
    1. 日/月顺序是靠什么确定的（数据证据 / 平台习惯）
    2. 有多少行日期认不出来（已排除，不是按当天补）
    3. 跨度是否离谱（可能是日序判断错了）
    """
    if "date" not in mapping:
        return None

    from app.core.timeline import detect_order, parse_date

    raw_values = [r.get("date") for r in rows]
    order, source = detect_order(raw_values)

    if source == "conflict":
        warnings.append(
            "日期列里同时出现了「日/月」和「月/日」两种写法（既有 25/10 也有 10/25），"
            "已按「日/月」解析，请核对源文件的日期格式"
        )
    assumed = False
    if order == "unknown":
        order = order_hint or "dmy"
        assumed = True
        if source == "no_evidence":
            warnings.append(
                f"日期全是「两段都不超过 12」的写法（如 01/11），无法从数据判断日/月顺序，"
                f"已按「{'日/月' if order == 'dmy' else '月/日'}」解析 —— 如果趋势图看起来错位，请检查这里"
            )

    unparsed = 0
    parsed_dates = []
    for r in rows:
        d = parse_date(r.get("date"), order=order)
        if d is None:
            unparsed += 1
            r.pop("date", None)
        else:
            r["date"] = d.isoformat()
            parsed_dates.append(d)

    if not parsed_dates:
        for r in rows:
            r.pop("date", None)
        warnings.append(
            f"「{mapping['date']}」这一列认不出日期，已忽略该列（示例：{str(raw_values[0])[:24]}）"
        )
        return None

    if unparsed:
        warnings.append(f"{unparsed} 行日期无法识别，这些行不参与趋势对比（不按当天补齐）")

    info = {
        "column": mapping["date"],
        "order": order,
        "order_source": source,
        "assumed": assumed,
        "unparsed": unparsed,
        "first": min(parsed_dates).isoformat(),
        "last": max(parsed_dates).isoformat(),
        "days": (max(parsed_dates) - min(parsed_dates)).days + 1,
    }

    if info["days"] > 730:
        warnings.append(
            f"日期跨度 {info['days']} 天，超过两年 —— 常见的日序解析错误会把 30 天数据摊成一年，建议核对"
        )
    return info


def parse_csv(raw: bytes, kind: str, order: str | None = None) -> dict:
    """解析并规范化一份 CSV。返回可直接入库与展示的结果。

    order 是「日/月」顺序的兜底假设，只在数据本身没有证据时生效
    （例如整列都是 01/11、02/11 这种两段都不超过 12 的写法）。
    调用方应按店铺所在平台传入：巴西/欧洲站 dmy，美国站 mdy。
    """
    if kind not in COLUMN_ALIASES:
        raise DatasetError(f"未知数据类型: {kind}")

    text = _decode(raw)
    if not text.strip():
        raise DatasetError("文件是空的")

    reader = csv.DictReader(io.StringIO(text))
    headers = [h for h in (reader.fieldnames or []) if h]
    if not headers:
        raise DatasetError("没有表头行，第一行必须是列名")

    mapping, ignored, missing = _map_columns(headers, kind)
    if missing:
        labels = "、".join(FIELD_LABELS.get(f, f) for f in missing)
        raise DatasetError(
            f"缺少必填列：{labels}。当前表头：{'、'.join(headers[:10])}"
        )

    rows: list[dict] = []
    warnings: list[str] = []
    skipped = 0

    numeric = NUMERIC_FIELDS[kind]
    for idx, raw_row in enumerate(reader, start=2):
        if len(rows) >= MAX_ROWS:
            warnings.append(f"超过 {MAX_ROWS} 行，只取前 {MAX_ROWS} 行")
            break

        # 字段数多于表头：通常是某个字段里有逗号却没加引号，会导致整行错位
        if None in raw_row:
            skipped += 1
            if len(warnings) < 5:
                warnings.append(f"第 {idx} 行字段数多于表头，可能某列含逗号但未加引号，已跳过")
            continue

        row: dict[str, Any] = {}
        bad = False
        for field, col in mapping.items():
            value = raw_row.get(col)
            if field in numeric:
                parsed = _to_number(value)
                if parsed is None:
                    if field in REQUIRED[kind]:
                        bad = True
                        if len(warnings) < 5:
                            warnings.append(f"第 {idx} 行「{FIELD_LABELS.get(field, field)}」不是数字：{value!r}")
                    continue
                row[field] = int(parsed) if field in INT_FIELDS else parsed
            else:
                row[field] = str(value or "").strip()

        if bad:
            skipped += 1
            continue
        rows.append(row)

    if not rows:
        raise DatasetError("没有解析出任何有效数据行，请检查数值列是否为空")

    date_info = _normalize_dates(rows, mapping, warnings, order)

    return {
        "ok": True,
        "kind": kind,
        "row_count": len(rows),
        "skipped": skipped,
        "rows": rows,
        "mapping": {FIELD_LABELS.get(k, k): v for k, v in mapping.items()},
        "ignored_columns": ignored,
        "warnings": warnings,
        "date_info": date_info,
        "preview": rows[:5],
    }


def build_payload(
    products: list[dict] | None = None,
    ads: list[dict] | None = None,
    reviews: list[dict] | None = None,
    meta: dict | None = None,
) -> dict:
    """把三份数据集组装成一次运行的 payload。

    只把**确实存在**的数据塞进去：缺哪份，下游 Stage 自己会报 INPUT_MISSING，
    而不是在这里编一份假的。
    """
    meta = meta or {}
    payload: dict[str, Any] = {
        "category": meta.get("category") or "未命名类目",
        "platform": meta.get("platform") or "shopee_br",
    }
    if products:
        payload["products"] = products
    if ads:
        payload["campaigns"] = ads
    if reviews:
        payload["reviews"] = reviews

    for key in ("product_cost", "target_price", "shipping_cost", "product_name",
                "target_audience", "selling_points", "keywords", "specs"):
        if meta.get(key):
            payload[key] = meta[key]

    return payload
