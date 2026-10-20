#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_facts.py — 统一事实数据包生成入口（v2.0，2026-10-20）
=============================================================
【定位】
替代 facts_builder.py 的直接调用，输出 v2 格式 facts JSON。
兼容 facts_builder.py 的采集+指标计算逻辑，但输出格式对齐
facts/2026-10-16.json 的 instruments 字典结构。

【v2 格式变更】
  - instruments: dict keyed by code（去掉前缀 sh/sz）
  - 新增 market 汇总字段
  - 新增 overall_quality / quality_errors
  - 新增 indicator_spec 口径说明

【输出】
  facts/{date}.json

【用法】
  python3 build_facts.py                     # 今天
  python3 build_facts.py --date 2026-10-16   # 指定交易日
"""
import argparse
import json
import os
import sys
from datetime import datetime

# 复用 facts_builder 的采集和计算逻辑
from facts_builder import (
    fetch_kline, rsi_wilder, macd_full, UNIVERSE
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FACTS_DIR = os.path.join(BASE_DIR, "facts")


def strip_prefix(sym):
    """sh601899 -> 601899, sz000858 -> 000858"""
    if sym.startswith(("sh", "sz")):
        return sym[2:]
    return sym


def build_one_v2(sym, name, sector, core, target_date, errors):
    """单标的采集+计算，返回 v2 格式字典。"""
    try:
        kl = fetch_kline(sym)
    except Exception as e:
        errors.append(f"{name}({sym}) 取数失败: {e}")
        return None

    hist = [r for r in kl if r["day"] <= target_date]
    if not hist or hist[-1]["day"] != target_date:
        errors.append(f"{name}({sym}) 新鲜度FAIL: 数据止于"
                      f"{hist[-1]['day'] if hist else '无'} != {target_date}")
        return None

    closes = [r["close"] for r in hist]
    vols = [r["volume"] for r in hist]
    close = closes[-1]
    prev = closes[-2] if len(closes) > 1 else close

    # 质量门
    gate_issues = []
    if not (0.1 < close < 10000) or abs(close / prev - 1) > 0.21:
        gate_issues.append("G1价格")
    if vols[-1] <= 0:
        gate_issues.append("G2成交量")
    if len(closes) < 40:
        gate_issues.append("G3历史不足40根")
    if gate_issues:
        errors.append(f"{name}({sym}) {'/'.join(gate_issues)} FAIL")
        return None

    r6 = rsi_wilder(closes, 6)
    macd = macd_full(closes)
    if len(closes) < 20:
        errors.append(f"{name}({sym}) MA20数据不足")
        return None
    if r6 is None or macd is None:
        errors.append(f"{name}({sym}) 指标计算失败")
        return None

    ma20 = round(sum(closes[-20:]) / 20, 3)
    ma5 = round(sum(closes[-5:]) / 5, 3) if len(closes) >= 5 else None
    ma10 = round(sum(closes[-10:]) / 10, 3) if len(closes) >= 10 else None

    # 判定 type
    item_type = "index" if sector == "大盘" else ("etf" if "ETF" in name else "stock")

    code_clean = strip_prefix(sym)
    return {
        "code": code_clean,
        "name": name,
        "type": item_type,
        "quality": "complete",
        "date": target_date,
        "close": close,
        "open": hist[-1]["open"],
        "high": hist[-1]["high"],
        "low": hist[-1]["low"],
        "change_pct": round((close / prev - 1) * 100, 2),
        "ten_day_chg": (round((close / closes[-11] - 1) * 100, 2)
                        if len(closes) > 11 else None),
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "rsi6": r6,
        "macd": macd,
        "vol_ratio": (round(vols[-1] / (sum(vols[-6:-1]) / 5), 2)
                      if len(vols) > 6 else None),
        "sector": sector,
        "core_pool": core,
    }


def build_v2(target_date):
    """生成 v2 格式 facts 数据包。"""
    errors = []
    instruments = {}
    market_info = None

    for sym, name, sector, core in UNIVERSE:
        item = build_one_v2(sym, name, sector, core, target_date, errors)
        if item is None:
            continue
        instruments[item["code"]] = item
        # 上证指数作为 market 汇总
        if sym == "sh000001":
            market_info = {
                "close": item["close"],
                "change_pct": item["change_pct"],
                "ten_day_chg": item["ten_day_chg"],
                "rsi6": item["rsi6"],
                "macd_signal": item["macd"]["state"],
            }

    overall_quality = "complete" if not errors else "partial"
    return {
        "date": target_date,
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "schema_version": "facts-v2.0",
        "indicator_spec": {
            "rsi": "RSI6, Wilder全历史递推",
            "macd": "EMA12/26全历史; DEA=EMA9(DIF); hist=DIF-DEA; state=近3日交叉事件或above/below",
            "vol_ratio": "当日成交量 / 前5日均量",
            "ten_day_chg": "相对10个交易日前收盘",
            "data_source": "新浪日K money.finance.sina.com.cn scale=240",
            "aligned_with": "WB 事实层统一口径 v2.0",
        },
        "instruments": instruments,
        "market": market_info or {},
        "overall_quality": overall_quality,
        "quality_errors": errors,
        "_prohibit": "本包为事实层，禁止写入 direction/action/kelly 等观点字段",
    }


def main():
    ap = argparse.ArgumentParser(description="生成统一事实数据包 v2")
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"),
                    help="目标交易日 YYYY-MM-DD（默认今天）")
    args = ap.parse_args()

    os.makedirs(FACTS_DIR, exist_ok=True)
    out = build_v2(args.date)
    dst = os.path.join(FACTS_DIR, f"{args.date}.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    n_inst = len(out["instruments"])
    quality = out["overall_quality"]
    n_err = len(out["quality_errors"])
    print(f"[{quality.upper()}] facts/{args.date}.json ({os.path.getsize(dst)}B)")
    print(f"     标的数: {n_inst} | 质量: {quality} | 错误: {n_err}项")

    # 打印核心池概要
    for code, inst in out["instruments"].items():
        if inst.get("core_pool"):
            m = inst["macd"]
            print(f"     {inst['name']:8s} close={inst['close']:8.2f} "
                  f"chg={inst['change_pct']:+6.2f}% "
                  f"rsi6={inst['rsi6']:6.2f} macd={m['state']}(hist={m['hist']})")

    if out["quality_errors"]:
        for e in out["quality_errors"]:
            print(f"     ⚠️ {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
