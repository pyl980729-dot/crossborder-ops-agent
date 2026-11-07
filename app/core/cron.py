"""最小 cron 解析器 —— 不引 APScheduler，标准库足够。

支持标准的 5 字段：分 时 日 月 周
  *        任意
  5        指定值
  1,15     枚举
  */10     步长
  9-18     范围

为什么自己写：
1. 依赖少一个是一个，clone 下来就能跑；
2. 调度需求很简单（每天一次 / 每小时一次），引入调度框架是过度设计；
3. 展示型项目里，能讲清「cron 是怎么算出下一次执行时间的」比调用现成库更有价值。
"""

from __future__ import annotations

from datetime import datetime, timedelta

MAX_LOOKAHEAD_DAYS = 70  # 够覆盖月度与年度任务


class CronError(ValueError):
    """cron 表达式非法。"""


def _parse_field(field: str, low: int, high: int) -> set[int]:
    """解析单个字段为取值集合。"""
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue

        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            try:
                step = int(step_text)
            except ValueError as exc:
                raise CronError(f"步长非法: {step_text}") from exc
            if step <= 0:
                raise CronError("步长必须大于 0")

        if part == "*":
            start, end = low, high
        elif "-" in part:
            a, _, b = part.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError as exc:
                raise CronError(f"范围非法: {part}") from exc
        else:
            try:
                start = end = int(part)
            except ValueError as exc:
                raise CronError(f"数值非法: {part}") from exc

        if start < low or end > high or start > end:
            raise CronError(f"超出取值范围 [{low},{high}]: {part}")

        values.update(range(start, end + 1, step))

    if not values:
        raise CronError(f"字段无有效取值: {field}")
    return values


class Cron:
    """解析后的 cron 表达式，可判断某个时间点是否命中、并推算下次执行。"""

    FIELDS = ("minute", "hour", "day", "month", "weekday")
    # 星期允许 0-7，其中 7 视同 0（周日）—— 这是 cron 的标准写法
    RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))

    def __init__(self, expr: str):
        parts = (expr or "").split()
        if len(parts) != 5:
            raise CronError("cron 必须是 5 个字段：分 时 日 月 周")
        self.expr = " ".join(parts)
        parsed = [_parse_field(p, lo, hi) for p, (lo, hi) in zip(parts, self.RANGES)]
        self.minutes, self.hours, self.days, self.months, self.weekdays = parsed
        # 7 视同 0（周日）
        if 7 in self.weekdays:
            self.weekdays.discard(7)
            self.weekdays.add(0)

    @staticmethod
    def _cron_weekday(dt: datetime) -> int:
        """把 Python 的 weekday() 转成 cron 的编号。

        Python: 周一=0 ... 周日=6
        cron  : 周日=0, 周一=1 ... 周六=6
        两个编号差一位，直接用 dt.weekday() 会让「每周一」跑到周二，这是必须避开的坑。
        """
        return (dt.weekday() + 1) % 7

    def matches(self, dt: datetime) -> bool:
        return (
            dt.minute in self.minutes
            and dt.hour in self.hours
            and dt.day in self.days
            and dt.month in self.months
            and self._cron_weekday(dt) in self.weekdays
        )

    def next_after(self, after: datetime, lookahead_days: int = MAX_LOOKAHEAD_DAYS) -> datetime | None:
        """从 after 之后找下一个命中时间（分钟精度）。

        逐层跳跃而不是逐分钟扫：
          日期不命中 → 跳到次日 00:00
          小时不命中 → 跳到下个整点
          分钟不命中 → 加 1 分钟
        每次迭代都严格向前推进，不会死循环，也足够快。
        """
        cursor = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = after + timedelta(days=lookahead_days)

        while cursor < limit:
            if (
                cursor.month not in self.months
                or cursor.day not in self.days
                or self._cron_weekday(cursor) not in self.weekdays
            ):
                cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if cursor.hour not in self.hours:
                cursor = (cursor + timedelta(hours=1)).replace(minute=0)
                continue
            if cursor.minute not in self.minutes:
                cursor += timedelta(minutes=1)
                continue
            return cursor
        return None

    def describe(self) -> str:
        """给人看的中文描述，用于前端展示。

        必须按「步长 → 具体值」的顺序判断：
        先判 isdigit 会把 */30 当成数字去 int()，直接抛异常。
        """
        if self.expr == "* * * * *":
            return "每分钟"

        m, h, d, mo, w = self.expr.split()
        simple_day = d == "*" and mo == "*"
        simple_week = w == "*"

        if m.startswith("*/") and h == "*" and simple_day and simple_week:
            return f"每 {m[2:]} 分钟"
        if h.startswith("*/") and m in ("0", "*") and simple_day and simple_week:
            return f"每 {h[2:]} 小时"
        if m.isdigit() and h.isdigit() and simple_day and simple_week:
            return f"每天 {int(h):02d}:{int(m):02d}"
        if h == "*" and m.isdigit() and simple_day and simple_week:
            return f"每小时第 {int(m)} 分"
        if m.isdigit() and h.isdigit() and simple_day and w != "*":
            days = {"0": "周日", "1": "周一", "2": "周二", "3": "周三", "4": "周四", "5": "周五", "6": "周六"}
            label = days.get(w, f"周{w}")
            return f"每{label} {int(h):02d}:{int(m):02d}"
        return self.expr


def validate(expr: str) -> tuple[bool, str]:
    """给接口层用的校验，返回 (是否合法, 描述或错误信息)。"""
    try:
        return True, Cron(expr).describe()
    except CronError as exc:
        return False, str(exc)
