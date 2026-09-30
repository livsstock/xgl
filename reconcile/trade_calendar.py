#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A 股交易日历  trade_calendar.py  v1.0
======================================
2026-09-29 建立。

【为什么必须要有这个模块】
此前所有回测里的"次日 / T+5 / 3日后"都是用自然日加减算的，导致两处硬错误：

  错误 1：9/24 → "次日"，实际不存在
    9/24(周四) 之后是 9/25 中秋休市、9/26(周六)、9/27(周日)，
    真正的下一交易日是 9/28 —— 跨了 4 个自然日。
    我们却在报告里写"次日跌 2.96%"，让大海按"次日"去核对，
    他会在 9/25 找不到数据，从而怀疑整份论证。

  错误 2：持有期被系统性高估
    A 股每年约 244 个交易日 / 365 个自然日 ≈ 0.67。
    用自然日算 "5日后"，实际只持有约 3.3 个交易日，
    收益被摊薄、大跌概率被低估。

【本模块的口径】
  - 交易日 = 非周末 且 非节假日（不含调休上班的周末，A 股调休日不开市）
  - 所有跨期计算一律走 count_trading_days / shift，不得用 datetime 直接加减

【数据来源】
  2026 年节假日依据国务院办公厅《关于2026年部分节假日安排的通知》。
  ⚠️ 若官方临时调整休市安排，需同步更新本文件并加版本号。

用法:
  from trade_calendar import shift, count_trading_days, is_trading_day
  shift("2026-09-24", 1)                 # -> "2026-09-28"  下一交易日
  count_trading_days("2026-09-21","2026-09-29")  # -> 6
"""

import datetime
from typing import List, Optional, Union

__version__ = "1.0"
CALENDAR_VERSION = "2026.1"   # 节假日表版本，写进预测文件便于追溯

DateLike = Union[str, datetime.date, datetime.datetime]

# ---------------------------------------------------------------- 节假日表

# 2026 年 A 股休市日（不含周末；周末由 weekday() 统一排除）
# 注意：调休补班的周末 A 股【不开市】，故一律不放回交易日集合。
HOLIDAYS_2026 = {
    # 元旦
    "2026-01-01", "2026-01-02",
    # 春节（2/16 除夕 ~ 2/22）
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
    "2026-02-23",
    # 清明
    "2026-04-06",
    # 劳动节
    "2026-05-01", "2026-05-04", "2026-05-05",
    # 端午
    "2026-06-19",
    # 中秋
    "2026-09-25",
    # 国庆（10/1~10/8，含中秋后连休）
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06",
    "2026-10-07", "2026-10-08",
}

# 历史/未来年份可在此追加，键为年份
_HOLIDAY_TABLE = {
    2026: HOLIDAYS_2026,
}

# 缓存，避免热路径反复构造
_TRADING_DAY_CACHE = {}


# ---------------------------------------------------------------- 基础工具

def _to_date(d: DateLike) -> datetime.date:
    """把各种输入统一成 date。str 支持 'YYYY-MM-DD' 及带时间部分的形式。"""
    if isinstance(d, datetime.datetime):
        return d.date()
    if isinstance(d, datetime.date):
        return d
    if isinstance(d, str):
        s = d.strip()
        # 截取前 10 位，容忍 "2026-09-29 15:00" / "2026-09-29收盘" 等写法
        m = s[:10]
        try:
            return datetime.date.fromisoformat(m)
        except ValueError:
            raise ValueError(f"无法解析日期：{d!r}（期望 YYYY-MM-DD 开头）")
    raise TypeError(f"不支持的日期类型：{type(d)}")


def _holidays(year: int) -> set:
    return _HOLIDAY_TABLE.get(year, set())


def is_trading_day(d: DateLike) -> bool:
    """是否交易日（非周末 且 非节假日）。"""
    dt = _to_date(d)
    if dt.weekday() >= 5:          # 5=周六 6=周日
        return False
    return dt.isoformat() not in _holidays(dt.year)


def is_weekend(d: DateLike) -> bool:
    return _to_date(d).weekday() >= 5


def is_holiday(d: DateLike) -> bool:
    """是否法定休市日（不含周末）。"""
    dt = _to_date(d)
    return dt.weekday() < 5 and dt.isoformat() in _holidays(dt.year)


def describe(d: DateLike) -> str:
    """给一天贴标签，用于日志/报告：交易日 / 周末 / 节假日(中秋 等)。"""
    dt = _to_date(d)
    if dt.weekday() >= 5:
        return "周末"
    if dt.isoformat() in _holidays(dt.year):
        return "节假日休市"
    return "交易日"


# ---------------------------------------------------------------- 位移

def shift(d: DateLike, n: int) -> str:
    """从 d 出发前进 n 个交易日（n 可为负），返回 'YYYY-MM-DD'。

    >>> shift("2026-09-24", 1)
    '2026-09-28'     # 跨中秋 + 周末
    >>> shift("2026-09-29", -1)
    '2026-09-28'
    >>> shift("2026-09-24", 0)
    '2026-09-24'     # n=0 时若 d 非交易日，仍原样返回（调用方自行判断）
    """
    dt = _to_date(d)
    if n == 0:
        return dt.isoformat()
    step = 1 if n > 0 else -1
    remain = abs(n)
    guard = 0
    while remain > 0:
        dt += datetime.timedelta(days=step)
        guard += 1
        if guard > 3650:           # 10 年还找不到，说明日历数据有问题
            raise RuntimeError(f"交易日位移超出预期：从 {d} 移动 {n} 天未结束")
        if is_trading_day(dt):
            remain -= 1
    return dt.isoformat()


def next_trading_day(d: DateLike) -> str:
    """d 之后的下一个交易日。"""
    return shift(d, 1)


def prev_trading_day(d: DateLike) -> str:
    """d 之前的上一个交易日。"""
    return shift(d, -1)


def align_to_trading_day(d: DateLike, forward: bool = True) -> str:
    """把任意一天吸附到最近的交易日。forward=True 向后找，否则向前找。"""
    dt = _to_date(d)
    if is_trading_day(dt):
        return dt.isoformat()
    step = 1 if forward else -1
    guard = 0
    while not is_trading_day(dt):
        dt += datetime.timedelta(days=step)
        guard += 1
        if guard > 30:
            raise RuntimeError(f"吸附交易日失败：{d}")
    return dt.isoformat()


# ---------------------------------------------------------------- 区间

def count_trading_days(start: DateLike, end: DateLike,
                       inclusive: bool = False) -> int:
    """统计 (start, end] 之间的交易日数（默认不含 start，含 end）。

    这就是"跨了几天"的正确算法：
      count_trading_days("2026-09-24", "2026-09-28") == 1   # 恰好 1 个交易日
    而自然日算法会得到 4，高估 4 倍。
    """
    s, e = _to_date(start), _to_date(end)
    if s > e:
        s, e = e, s
    n = 0
    cur = s
    if not inclusive:
        cur += datetime.timedelta(days=1)
    while cur <= e:
        if is_trading_day(cur):
            n += 1
        cur += datetime.timedelta(days=1)
    return n


def trading_days_between(start: DateLike, end: DateLike,
                         inclusive_start: bool = False) -> List[str]:
    """列出 (start, end] 内的全部交易日（升序），供逐日回测使用。"""
    s, e = _to_date(start), _to_date(end)
    if s > e:
        s, e = e, s
    out = []
    cur = s
    if not inclusive_start:
        cur += datetime.timedelta(days=1)
    while cur <= e:
        if is_trading_day(cur):
            out.append(cur.isoformat())
        cur += datetime.timedelta(days=1)
    return out


def last_trading_day(today: Optional[DateLike] = None,
                     before_close: bool = False) -> str:
    """最近一个【已完成】的交易日。

    before_close=False（默认）：若今天是交易日，返回今天（收盘后使用）。
    before_close=True：若今天是交易日，返回上一个交易日（盘中/盘前使用，
                        因为今天的收盘价还没出来）。
    """
    dt = _to_date(today) if today else datetime.date.today()
    if is_trading_day(dt) and not before_close:
        return dt.isoformat()
    return shift(dt, -1)


# ---------------------------------------------------------------- 自检

def _selftest():
    """关键用例自检——每次改动日历后跑一遍。"""
    cases = [
        # (起点, n, 期望, 说明)
        ("2026-09-24", 1, "2026-09-28", "跨中秋+周末"),
        ("2026-09-24", 2, "2026-09-29", ""),
        ("2026-09-21", 1, "2026-09-22", "普通相邻交易日"),
        ("2026-09-28", 1, "2026-09-29", ""),
        ("2026-09-29", 1, "2026-09-30", ""),
        ("2026-09-29", 5, "2026-10-14", "跨国庆长假"),
        ("2026-09-30", 1, "2026-10-09", "国庆后首个交易日"),
        ("2026-09-29", -1, "2026-09-28", "往前一天"),
        ("2026-10-09", -1, "2026-09-30", "往前跨国庆"),
    ]
    bad = 0
    for start, n, expect, note in cases:
        got = shift(start, n)
        flag = "✅" if got == expect else "❌"
        if got != expect:
            bad += 1
        print(f"  {flag} shift({start}, {n}) = {got}   期望 {expect}  {note}")

    # 间隔计数
    n = count_trading_days("2026-09-24", "2026-09-28")
    ok = (n == 1)
    print(f"  {'✅' if ok else '❌'} count_trading_days(9/24→9/28) = {n}  期望 1")
    if not ok:
        bad += 1

    # 非交易日识别
    for d, expect in [("2026-09-25", False), ("2026-09-26", False),
                      ("2026-09-27", False), ("2026-09-28", True),
                      ("2026-10-01", False), ("2026-10-09", True)]:
        got = is_trading_day(d)
        ok = (got == expect)
        if not ok:
            bad += 1
        print(f"  {'✅' if ok else '❌'} is_trading_day({d}) = {got}"
              f"  期望 {expect}  ({describe(d)})")

    print(f"\n自检结果：{'全部通过' if bad == 0 else f'{bad} 项失败'}")
    return bad


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv or len(sys.argv) == 1:
        print(f"trade_calendar v{__version__}  日历版本 {CALENDAR_VERSION}")
        print("=" * 60)
        sys.exit(1 if _selftest() else 0)
