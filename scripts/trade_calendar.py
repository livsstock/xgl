#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A 股交易日历  trade_calendar.py  v2.0
======================================
单一日历源：所有需要判断交易日的脚本统一 import 本模块。
用法:
  from scripts.trade_calendar import is_trading_day, shift, next_trading_day, beijing_today
  is_trading_day("2026-10-01")  # -> False
  shift("2026-09-24", 1)        # -> "2026-09-28"
  beijing_today()                # -> datetime.date

2026-10-01 v2.0: 从 reconcile/ 提升到 scripts/，作为唯一日历源
"""

import datetime
from typing import List, Optional, Union

__version__ = "2.0"
CALENDAR_VERSION = "2026.1"

DateLike = Union[str, datetime.date, datetime.datetime]

TZ_CST = datetime.timezone(datetime.timedelta(hours=8))

# ---------------------------------------------------------------- 节假日表
# 2026 年 A 股休市日（不含周末；周末由 weekday() 统一排除）
# 数据来源：国务院办公厅《关于2026年部分节假日安排的通知》
HOLIDAYS_2026 = {
    # 元旦
    "2026-01-01", "2026-01-02", "2026-01-03",
    # 春节
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
    "2026-02-21", "2026-02-22",
    # 清明
    "2026-04-04", "2026-04-05", "2026-04-06",
    # 劳动节
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    # 端午
    "2026-05-31", "2026-06-01", "2026-06-02",
    # 中秋 + 国庆
    "2026-09-25", "2026-09-26", "2026-09-27",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
}

_HOLIDAY_TABLE = {2026: HOLIDAYS_2026}


# ---------------------------------------------------------------- 核心函数

def _to_date(d: DateLike) -> datetime.date:
    if isinstance(d, datetime.datetime):
        return d.date()
    if isinstance(d, datetime.date):
        return d
    if isinstance(d, str):
        s = d.strip()[:10]
        try:
            return datetime.date.fromisoformat(s)
        except ValueError:
            raise ValueError(f"无法解析日期：{d!r}")
    raise TypeError(f"不支持的类型：{type(d)}")


def beijing_today() -> datetime.date:
    """返回当前北京时间（UTC+8）的日期"""
    return datetime.datetime.now(TZ_CST).date()


def is_trading_day(d: DateLike) -> bool:
    """是否 A 股交易日（非周末 且 非节假日）"""
    dt = _to_date(d)
    if dt.weekday() >= 5:
        return False
    return dt.isoformat() not in _HOLIDAY_TABLE.get(dt.year, set())


def is_weekend(d: DateLike) -> bool:
    return _to_date(d).weekday() >= 5


def is_holiday(d: DateLike) -> bool:
    """是否法定休市日（不含周末）"""
    dt = _to_date(d)
    return dt.weekday() < 5 and dt.isoformat() in _HOLIDAY_TABLE.get(dt.year, set())


def describe(d: DateLike) -> str:
    dt = _to_date(d)
    if dt.weekday() >= 5:
        return "周末"
    if dt.isoformat() in _HOLIDAY_TABLE.get(dt.year, set()):
        # 尝试给出节日名称
        name_map = {
            "01-01": "元旦", "02-16": "春节", "02-17": "春节", "02-18": "春节",
            "02-19": "春节", "02-20": "春节", "02-21": "春节", "02-22": "春节",
            "04-04": "清明", "04-05": "清明", "04-06": "清明",
            "05-01": "劳动节", "05-02": "劳动节", "05-03": "劳动节",
            "05-04": "劳动节", "05-05": "劳动节",
            "05-31": "端午", "06-01": "端午", "06-02": "端午",
            "09-25": "中秋", "09-26": "中秋", "09-27": "中秋",
            "10-01": "国庆", "10-02": "国庆", "10-03": "国庆", "10-04": "国庆",
            "10-05": "国庆", "10-06": "国庆", "10-07": "国庆", "10-08": "国庆",
        }
        suffix = dt.strftime("%m-%d")
        holiday_name = name_map.get(suffix, "节假日")
        return f"节假日休市({holiday_name})"
    return "交易日"


def shift(d: DateLike, n: int) -> str:
    """从 d 出发前进 n 个交易日，返回 'YYYY-MM-DD'"""
    dt = _to_date(d)
    if n == 0:
        return dt.isoformat()
    step = 1 if n > 0 else -1
    remain = abs(n)
    guard = 0
    while remain > 0:
        dt += datetime.timedelta(days=step)
        guard += 1
        if guard > 3650:
            raise RuntimeError(f"交易日位移超出预期：从 {d} 移动 {n} 天未结束")
        if is_trading_day(dt):
            remain -= 1
    return dt.isoformat()


def next_trading_day(d: DateLike) -> str:
    return shift(d, 1)


def prev_trading_day(d: DateLike) -> str:
    return shift(d, -1)


def count_trading_days(start: DateLike, end: DateLike, inclusive: bool = False) -> int:
    """统计 (start, end] 之间的交易日数"""
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


def trading_days_between(start: DateLike, end: DateLike, inclusive_start: bool = False) -> List[str]:
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


def last_trading_day(today: Optional[DateLike] = None, before_close: bool = False) -> str:
    """最近一个已完成的交易日"""
    dt = _to_date(today) if today else beijing_today()
    if is_trading_day(dt) and not before_close:
        return dt.isoformat()
    return shift(dt, -1)


def should_run_today(today: Optional[DateLike] = None) -> bool:
    """
    供 workflow 调用：今天是否应该跑采集/对账？
    返回 True 表示交易日，False 表示应跳过。
    """
    dt = _to_date(today) if today else beijing_today()
    return is_trading_day(dt)


# ---------------------------------------------------------------- 自检

def _selftest() -> int:
    cases = [
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

    n = count_trading_days("2026-09-24", "2026-09-28")
    ok = (n == 1)
    print(f"  {'✅' if ok else '❌'} count_trading_days(9/24→9/28) = {n}  期望 1")
    if not ok:
        bad += 1

    for d, expect in [("2026-09-25", False), ("2026-09-26", False),
                      ("2026-09-27", False), ("2026-09-28", True),
                      ("2026-10-01", False), ("2026-10-09", True)]:
        got = is_trading_day(d)
        ok = (got == expect)
        if not ok:
            bad += 1
        print(f"  {'✅' if ok else '❌'} is_trading_day({d}) = {got}"
              f"  期望 {expect}  ({describe(d)})")

    print(f"\n自检结果：{'全部通过 ✅' if bad == 0 else f'{bad} 项失败 ❌'}")
    return bad


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv or len(sys.argv) == 1:
        print(f"trade_calendar v{__version__}  日历版本 {CALENDAR_VERSION}")
        print("=" * 60)
        sys.exit(1 if _selftest() else 0)
