"""时间轴 —— 把「一张汇总报表」变成「一条趋势线」。

没有时间轴时，系统只能回答「这堆数据整体怎么样」。
有了时间轴，才能回答三个真正决定动作的问题：

1. 本期比上期好了还是差了
2. 哪个广告组在恶化（不被整体平均值掩盖）
3. 上次做的改动，在数据上体现出来了吗

三个必须处理的坑，每一个都会让结论反向：

**坑 1：日期写法跨国不同。**
`01/11/2026` 在巴西是 11 月 1 日，在美国是 1 月 11 日。
解析错了不会报错，只会把 30 天数据压成 11 个月 —— 趋势图变成噪声。
所以先拿整个数据集找证据（有没有出现大于 12 的日子），找不到再按平台习惯兜底，
并把「做了什么假设」明确写进结果。

**坑 2：周期必须等长。**
今天是周三。拿「本周 3 天」跟「上周完整 7 天」比，必然显示暴跌 50% 以上。
这是运营日报里最常见的假结论。所以用**同长度滚动窗口**：
本期取最近 7 天，上期取再往前 7 天，跟自然周无关。

**坑 3：缺天不等于零。**
某天没有数据，可能是没投放，也可能是漏导了一天。
两者的处理完全相反：前者是真实业务波动，后者是数据错误。
所以统计「有数据的天数」，覆盖率不够就不做对比，而不是当成 0 算进去。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Callable, Sequence

# 趋势图最多画多少天，防止一份两年的报表把 JSON 撑爆
MAX_SERIES_DAYS = 120

# 英文与葡语的月份缩写。巴西站导出常见 "01/out/2026" 这种写法
_MONTHS: dict[str, int] = {
    "jan": 1, "feb": 2, "fev": 2, "mar": 3, "apr": 4, "abr": 4,
    "may": 5, "mai": 5, "jun": 6, "jul": 7, "aug": 8, "ago": 8,
    "sep": 9, "sept": 9, "set": 9, "oct": 10, "out": 10,
    "nov": 11, "dec": 12, "dez": 12,
}

_RE_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_RE_YMD = re.compile(r"^(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
_RE_DMY_MDY = re.compile(r"^(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{2,4})")
# 月份名允许带重音（março）和连接词（7 de novembro de 2026）
_LETTERS = r"[A-Za-z\u00c0-\u024f]{3,9}"
_CONNECTOR = r"(?:\s*(?:de|del|of|da)\s*)?"
_RE_D_MON_Y = re.compile(
    rf"^(\d{{1,2}}){_CONNECTOR}[\s\-/]*({_LETTERS})\.?{_CONNECTOR}[\s\-/,]*(\d{{2,4}})$"
)
_RE_MON_D_Y = re.compile(
    rf"^({_LETTERS})\.?{_CONNECTOR}[\s\-/]*(\d{{1,2}}){_CONNECTOR}[\s\-/,]*(\d{{2,4}})$"
)


def _month_of(token: str) -> int | None:
    """月份名查表：只取前三个字母，兼容 nov / novembro / november。"""
    return _MONTHS.get(token.strip().lower()[:3])


def _full_year(value: int) -> int:
    if value >= 100:
        return value
    # 两位年份：跨境报表基本不会出现 1969 年的数据
    return 2000 + value if value <= 68 else 1900 + value


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def extract(value: Any) -> tuple[str, tuple[int, int, int]] | None:
    """从一段文本里认出日期结构，返回 (形式, 三个数字)。

    形式决定三个数字的含义：
      ymd / compact  → (年, 月, 日)
      dmy_mdy        → (第一段, 第二段, 年)，谁大谁小未知
      d_mon_y        → (日, 月, 年)
      mon_d_y        → (月, 日, 年)
    """
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return ("ymd", (value.year, value.month, value.day))

    text = str(value).strip()
    if not text:
        return None

    m = _RE_COMPACT.match(text)
    if m:
        return ("compact", (int(m.group(1)), int(m.group(2)), int(m.group(3))))

    m = _RE_YMD.match(text)
    if m:
        return ("ymd", (int(m.group(1)), int(m.group(2)), int(m.group(3))))

    m = _RE_DMY_MDY.match(text)
    if m:
        return ("dmy_mdy", (int(m.group(1)), int(m.group(2)), _full_year(int(m.group(3)))))

    m = _RE_D_MON_Y.match(text)
    if m:
        month = _month_of(m.group(2))
        if month:
            return ("d_mon_y", (int(m.group(1)), month, _full_year(int(m.group(3)))))

    m = _RE_MON_D_Y.match(text)
    if m:
        month = _month_of(m.group(1))
        if month:
            return ("mon_d_y", (month, int(m.group(2)), _full_year(int(m.group(3)))))

    return None


def detect_order(values: Sequence[Any]) -> tuple[str, str]:
    """用整个数据集判断「01/11」是 日/月 还是 月/日。

    返回 (order, source)：
      order  ∈ "dmy" / "mdy" / "unknown"
      source ∈ "data"（数据里有大于 12 的日子，可确认）/ "conflict" / "no_evidence"

    conflict 意味着同一列里既出现过「第一段 > 12」又出现过「第二段 > 12」，
    说明这一列混了两种写法 —— 这种情况不能猜，必须报出来让人修源文件。
    """
    dmy = mdy = False
    for raw in values:
        got = extract(raw)
        if not got or got[0] != "dmy_mdy":
            continue
        first, second, _ = got[1]
        if first > 12:
            dmy = True
        if second > 12:
            mdy = True

    if dmy and mdy:
        return "unknown", "conflict"
    if dmy:
        return "dmy", "data"
    if mdy:
        return "mdy", "data"
    return "unknown", "no_evidence"


def parse_date(value: Any, order: str = "dmy") -> date | None:
    """解析成 date。认不出返回 None —— 不猜、不填当天日期。"""
    got = extract(value)
    if not got:
        return None

    form, nums = got
    if form in ("ymd", "compact"):
        return _safe_date(nums[0], nums[1], nums[2])
    if form == "d_mon_y":
        return _safe_date(nums[2], nums[1], nums[0])
    if form == "mon_d_y":
        return _safe_date(nums[2], nums[0], nums[1])

    first, second, year = nums
    if first > 12 and second <= 12:
        return _safe_date(year, second, first)
    if second > 12 and first <= 12:
        return _safe_date(year, first, second)
    if order == "mdy":
        return _safe_date(year, first, second)
    return _safe_date(year, second, first)


def _coverage(dates: list[date], start: date, end: date) -> tuple[float, int]:
    """窗口内的覆盖率 = 有数据的天数 / 窗口天数。"""
    total_days = (end - start).days + 1
    if total_days <= 0:
        return 0.0, 0
    present = len({d for d in dates if start <= d <= end})
    return round(present / total_days, 4), present


def analyze(
    rows: Sequence[dict],
    *,
    date_key: str = "date",
    window: int = 7,
    order: str = "dmy",
    series_fn: Callable[[list[dict]], dict] | None = None,
    min_coverage: float = 0.6,
) -> dict:
    """切出「本期 / 上期」两个等长窗口，并给出可对比的结构。

    rows 里每行的 date_key 可以是 date 对象、ISO 字符串或各种本地写法。
    series_fn 传入一组同一天的行，返回当天的指标 —— 让趋势图的计算
    留在调用方（广告看 ROAS，评论看好评率），本模块不掺业务。
    """
    parsed: list[tuple[date, dict]] = []
    unparsed = 0
    for row in rows:
        d = parse_date(row.get(date_key), order=order)
        if d is None:
            unparsed += 1
            continue
        parsed.append((d, row))

    base = {
        "available": False,
        "reason": "",
        "window_days": window,
        "assumption": "",
        "rows_total": len(rows),
        "rows_with_date": len(parsed),
        "rows_without_date": unparsed,
        "current": None,
        "previous": None,
        "series": [],
    }

    if not parsed:
        base["reason"] = "数据里没有可识别的日期，无法做趋势对比，只能按整表汇总"
        return base

    dates = [d for d, _ in parsed]
    first, last = min(dates), max(dates)
    span = (last - first).days + 1

    base["range"] = {
        "first": first.isoformat(),
        "last": last.isoformat(),
        "span_days": span,
        "days_with_data": len(set(dates)),
        "missing_days": span - len(set(dates)),
    }
    if unparsed:
        base["assumption"] = f"{unparsed} 行日期无法识别，已排除在对比之外"

    if span < window * 2:
        base["reason"] = (
            f"数据只覆盖 {span} 天，同长度对比需要 {window * 2} 天"
            f"（本期 {window} 天 + 上期 {window} 天）"
        )
        return base

    cur_start = last - timedelta(days=window - 1)
    prev_end = cur_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=window - 1)

    cur_rows = [r for d, r in parsed if cur_start <= d <= last]
    prev_rows = [r for d, r in parsed if prev_start <= d <= prev_end]

    cur_cov, cur_days = _coverage(dates, cur_start, last)
    prev_cov, prev_days = _coverage(dates, prev_start, prev_end)

    base["current"] = {
        "start": cur_start.isoformat(),
        "end": last.isoformat(),
        "row_count": len(cur_rows),
        "coverage": cur_cov,
        "days_with_data": cur_days,
        "rows": cur_rows,
    }
    base["previous"] = {
        "start": prev_start.isoformat(),
        "end": prev_end.isoformat(),
        "row_count": len(prev_rows),
        "coverage": prev_cov,
        "days_with_data": prev_days,
        "rows": prev_rows,
    }

    if cur_cov < min_coverage or prev_cov < min_coverage:
        base["reason"] = (
            f"可用天数不足（本期 {cur_days}/{window} 天、上期 {prev_days}/{window} 天），"
            f"低于 {min_coverage:.0%} 的对比会失真 —— 涨跌主要反映「投放了几天」而不是「效果好不好」"
        )
        return base

    # 趋势序列：按天分桶，交给 series_fn 算当天的业务指标
    if series_fn is not None:
        buckets: dict[date, list[dict]] = {}
        for d, row in parsed:
            buckets.setdefault(d, []).append(row)

        start = max(first, last - timedelta(days=MAX_SERIES_DAYS - 1))
        cursor = start
        series = []
        while cursor <= last:
            day_rows = buckets.get(cursor)
            point: dict[str, Any] = {"date": cursor.isoformat(), "has_data": bool(day_rows)}
            if day_rows:
                try:
                    point.update(series_fn(day_rows))
                except Exception:  # noqa: BLE001 - 单天算不出就留空，不拖垮整张图
                    point["has_data"] = False
            series.append(point)
            cursor += timedelta(days=1)
        base["series"] = series

    base["available"] = True
    if not base["assumption"]:
        base["assumption"] = "日期按「日/月/年」解析" if order == "dmy" else "日期按「月/日/年」解析"
    return base


def group_delta(
    current_rows: Sequence[dict],
    previous_rows: Sequence[dict],
    *,
    key: str,
    value_fn: Callable[[list[dict]], float],
    min_rows: int = 1,
) -> list[dict]:
    """逐组对比（广告组、评论类别…），用于找出「谁在恶化」。

    只输出两组都有足够样本的对象：本期新出现的组没有上期基线，
    拿它算涨跌只会得到「从 0 暴涨」这种噪声。
    """
    def bucket(rows: Sequence[dict]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for r in rows:
            name = str(r.get(key) or "").strip()
            if name:
                out.setdefault(name, []).append(r)
        return out

    cur, prev = bucket(current_rows), bucket(previous_rows)
    out = []
    for name in prev:
        if name not in cur:
            continue
        if len(prev[name]) < min_rows or len(cur[name]) < min_rows:
            continue
        before = value_fn(prev[name])
        after = value_fn(cur[name])
        delta = after - before
        out.append(
            {
                "name": name,
                "prev": round(before, 4),
                "curr": round(after, 4),
                "delta": round(delta, 4),
                "delta_pct": round(delta / before, 4) if before else 0.0,
                "changed": abs(delta) > 1e-9,
            }
        )
    return out
