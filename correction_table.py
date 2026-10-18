#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
correction_table.py — 双模型分歧的修正规则（v1.0，2026-10-18）
==============================================================
【依据】
backtest/signal_stats.py 实测（10标的/7200根K线）：
  FDR 校正后 0/24 信号显著 → 单信号都没有可靠的 edge。
  相对最优：RSI6<15 深超卖 T+3 胜率57.0%/均收+1.00% (n=151)
  基本无效：RSI6<25 超卖 T+3 胜率48.3%/均收+0.10% (n=646)  ← 此前主力信号
  样本不足：超卖+MACD金叉 83.3% 但仅 n=6，不作依据

【修正规则（替代"分歧就不动"）】
  方向：一律不因单方观点改方向（双方都无显著 edge，改方向=赌博）
  仓位：按信号级别的实测胜率给"信心系数"

    confidence:
      1.0  双方一致 AND 命中强信号(RSI6<15)
      0.7  双方一致，但信号一般
      0.5  真分歧（输入一致、判断不同）→ 各让一半
      0.3  单方命中弱信号、另一方无信号
      0.0  数据分歧 / 命中已知无效信号(RSI6 在 15~25 区间)

  仓位 = 基础凯利 × confidence，再走硬约束（≤20%单标的 / ≤60%总仓 / <2%不操作）

【用法】
  python3 correction_table.py --date 2026-10-16
  python3 correction_table.py --date 2026-10-16 --push
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATS = os.path.join(BASE_DIR, "backtest", "signal_stats.json")
OUT_DIR = os.path.join(BASE_DIR, "reconcile")

# 实测得来的信号效力分档（每次重跑 signal_stats 后手工复核更新）
SIGNAL_POWER = {
    "strong": {"RSI6<15": {"t3_win": 57.0, "t3_ret": 1.00, "n": 151}},
    "weak": {
        "RSI6<25": {"t3_win": 48.3, "t3_ret": 0.10, "n": 646},
        "MACD金叉": {"t3_win": 51.2, "t3_ret": 0.37, "n": 884},
        "10日跌超10%": {"t3_win": 50.5, "t3_ret": 0.50, "n": 396},
        "MACD空头加速": {"t3_win": 47.8, "t3_ret": 0.21, "n": 1966},
    },
    "invalid": {"RSI6 15~25 区间": {"t3_win": 48.3, "t3_ret": 0.10, "n": 646}},
}


def classify_signal(facts_stock):
    """根据事实包里的指标，判定该标的命中的信号强度"""
    rsi = facts_stock.get("rsi6")
    macd = (facts_stock.get("macd") or {}).get("state", "")
    if rsi is None:
        return "unknown", None
    if rsi < 15:
        return "strong", f"RSI6={rsi}<15（实测T+3胜率57%，唯一有边缘）"
    if 15 <= rsi < 25:
        return "invalid", f"RSI6={rsi} 落在15~25区间（实测胜率48.3%，无边缘）"
    if macd in ("golden_cross",):
        return "weak", f"MACD金叉（实测胜率51.2%，接近随机）"
    return "none", f"无有效信号（RSI6={rsi}, MACD={macd}）"


def decide(wb_action, dh_action, power, same_semantic, data_ok):
    """核心修正逻辑：返回 (confidence, 理由)"""
    if not data_ok:
        return 0.0, "数据分歧 → 不出手"
    if power == "strong":
        if same_semantic and wb_action in ("buy",):
            return 1.0, "双方一致 + 命中强信号(RSI6<15) → 足额"
        if wb_action == "buy" or dh_action == "buy":
            return 0.6, "强信号，但双方未完全一致 → 六成"
    if power == "invalid":
        return 0.0, "落在已知无效区间(RSI6 15~25) → 不出手"
    if same_semantic:
        if wb_action == "buy":
            return 0.7, "双方一致买入，但信号一般 → 七成"
        return 0.0, "双方一致不买 → 无仓位"
    # 真分歧
    return 0.5, "真分歧（输入一致、判断不同）→ 各让一半"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)

    # 加载事实 / WB观点 / 对账结果
    fp = os.path.join(BASE_DIR, "facts", f"{args.date}.json")
    cp = os.path.join(OUT_DIR, "daily_compare.json")
    if not os.path.exists(fp):
        print(f"[ERR] 缺事实包 {fp}，先跑 facts_builder.py --date {args.date}")
        return 1
    facts = json.load(open(fp, encoding="utf-8"))
    fm = {s["name"]: s for s in facts["stocks"]}

    # 对账结果里已含双方 action 与归因
    comp = None
    if os.path.exists(cp):
        arr = json.load(open(cp, encoding="utf-8"))
        for r in arr:
            if r.get("date") == args.date:
                comp = r
    if not comp or comp.get("status") != "ok":
        print("[ERR] 缺当日对账结果，先跑 reconcile_wb_dahai.py --dates " + args.date)
        return 1

    rows = []
    for s in comp["stocks"]:
        name = s["name"]
        f = fm.get(name) or fm.get("半导体ETF" if "半导体" in name else name)
        if not f:
            continue
        power, why_signal = classify_signal(f)
        data_ok = (s.get("attribution", "").startswith("一致")
                   or s.get("attribution", "").startswith("真分歧")
                   or s.get("attribution", "").startswith("无事实包") is False
                   and "数据分歧" not in s.get("attribution", ""))
        conf, why = decide(s["wb_action"], s["dahai_action"], power,
                           s["same_semantic"], data_ok)
        kelly = s.get("kelly_pct") or 0
        # 若双方均未给凯利（如 hold/watch），但命中强信号，用基准凯利补一个底仓
        if not kelly and power == "strong":
            kelly = 6.0   # A级信号基准（全凯利约12% × 半凯利）
        final = round(kelly * conf, 2) if kelly else 0.0
        if 0 < final < 2.0:
            final = 0.0
            why += "；仓位<2%门槛，不操作"
        rows.append({
            "name": name, "wb_action": s["wb_action"], "dahai_action": s["dahai_action"],
            "same_semantic": s["same_semantic"], "attribution": s.get("attribution"),
            "rsi6": f.get("rsi6"), "macd": (f.get("macd") or {}).get("state"),
            "signal_power": power, "signal_note": why_signal,
            "base_kelly_pct": kelly, "confidence": conf,
            "final_position_pct": final, "decision_reason": why,
        })

    out = {
        "date": args.date,
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "_basis": "signal_stats.json 实测：FDR 0/24 显著；RSI6<15 是唯一有边缘的信号",
        "_principle": "不改方向只调仓位；数据分歧与无效区间一律不出手",
        "positions": rows,
        "total_position_pct": round(sum(r["final_position_pct"] for r in rows), 2),
    }
    dst = os.path.join(OUT_DIR, f"correction-{args.date}.json")
    json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"\n{'='*74}")
    print(f"修正规则裁决  {args.date}")
    print(f"{'='*74}")
    print(f"{'标的':10s} {'WB':6s} {'大海':6s} {'RSI6':>6s} {'信号':8s} {'信心':>5s} {'仓位':>6s}")
    for r in rows:
        print(f"{r['name'][:9]:10s} {r['wb_action']:6s} {r['dahai_action']:6s} "
              f"{r['rsi6'] or 0:6.2f} {r['signal_power']:8s} "
              f"{r['confidence']:5.1f} {r['final_position_pct']:5.2f}%")
    print(f"\n总仓位建议: {out['total_position_pct']}%")
    print(f"已写入 {dst}")

    if args.push:
        r = subprocess.run([sys.executable, os.path.join(BASE_DIR, "push_api.py"),
                            f"reconcile/correction-{args.date}.json"],
                           capture_output=True, text=True, timeout=90)
        print("推送:", (r.stdout or "").strip().splitlines()[-1] if r.stdout else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
