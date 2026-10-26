#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_opinions_wb.py — WB 观点导出器（opinion-v1.0，2026-10-26）

【背景】大海 14:45 尾盘复核需要 opinions-wb/{date}_wb.json（schema opinion-v1.0），
此前 WB 只推 predictions/，该目录自 10-16 起断更，导致大海复核多次中止。

【逻辑】
  输入: predictions/{T}.json（T 收盘数据预测 T+1）+ facts/{T}.json
  输出: opinions-wb/{T+1}_wb.json（date=T+1, based_on_facts=T）
  14:45 复核当天读取的文件 date 应为当天。

【schema】model/date/based_on_facts/market/calls[{
    code,name,direction,action,confidence,kelly_grade,kelly_pct,
    evidence{close,change_pct,ten_day_chg,rsi6,macd_signal},reason}]
  R1 action∈buy/sell/hold/watch  R3 buy必须有evidence
  R4~R5 evidence 数值与 facts 对齐  R6 rsi6∈[0,100]  R7 macd_signal与DIF/DEA一致

用法: python3 export_opinions_wb.py --date 2026-10-23
"""
import argparse
import json
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "reconcile"))
import trade_calendar as TC  # noqa: E402

PRED = os.path.join(BASE, "predictions")
FACTS = os.path.join(BASE, "facts")
OUT = os.path.join(BASE, "opinions-wb")

GRADE = {"buy": "B", "buy_probe": "C", "watch": "C", "hold": "D", "avoid": "D"}
DIRMAP = {"buy": "bullish", "buy_probe": "bullish", "hold": "neutral", "watch": "neutral", "avoid": "bearish"}
# R1 词表: buy/sell/hold/watch（buy_probe 归并为 buy_probe→watch+提示，avoid→sell语义? 统一: avoid→hold并reason说明）
ACTION = {"buy": "buy", "buy_probe": "watch", "watch": "watch", "hold": "hold", "avoid": "sell"}


def load(p):
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def macd_signal(m):
    """R7: 直接采用 facts 的 macd.state（above/below/golden_cross/death_cross），
    保证与校验器判定完全一致。"""
    if isinstance(m, dict):
        return m.get("state")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="预测生成日 T（收盘数据日）")
    args = ap.parse_args()
    T = args.date
    t1 = TC.next_trading_day(T)

    wb = load(os.path.join(PRED, f"{T}.json"))
    facts = load(os.path.join(FACTS, f"{T}.json"))
    if not wb or not facts:
        print(f"[OPINION] 缺 predictions/{T}.json 或 facts/{T}.json，不产出")
        return 1

    finst = facts.get("instruments") or {}
    mo = wb.get("market_outlook") or {}
    calls = []
    for c in wb.get("stock_calls") or []:
        code = (c.get("code") or "").replace("sh", "").replace("sz", "")
        f = finst.get(code) or {}
        action = ACTION.get(c.get("action"), "hold")
        rsi6 = f.get("rsi6") if f.get("rsi6") is not None else c.get("rsi6")
        calls.append({
            "code": code,
            "name": c.get("name"),
            "direction": DIRMAP.get(c.get("action"), "neutral"),
            "action": action,
            "confidence": c.get("confidence", 50),
            "kelly_grade": GRADE.get(c.get("action"), "D"),
            "kelly_pct": c.get("suggest_position_pct") or 0,
            "evidence": {
                "close": f.get("close"),
                "change_pct": f.get("change_pct"),
                "ten_day_chg": f.get("ten_day_chg"),
                "rsi6": rsi6,
                "macd_signal": macd_signal(f.get("macd")),
            },
            "reason": (c.get("reason") or "")[:200],
        })

    out = {
        "model": "workbuddy",
        "model_version": wb.get("model_version", "WB-v2.5"),
        "date": t1,
        "based_on_facts": T,
        "schema_version": "opinion-v1.0",
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "market": {
            "direction": mo.get("direction", "neutral"),
            "confidence": mo.get("confidence", 50),
            "reason": (mo.get("reason") or "")[:300],
        },
        "calls": calls,
        "_note": "由 export_opinions_wb.py 自动生成；14:45 复核请读本文件。08:36/14:30 双时点决策另见 predictions/decision_points/。",
    }

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{t1}_wb.json")
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=2)
    print(f"[OPINION] 已生成 {path}（calls={len(calls)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
