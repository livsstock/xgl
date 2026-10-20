#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
same_basis_reconcile.py — 同基准对账（2026-10-20 新增）
========================================================
【为什么需要】
WB 与大海的跑批时点不同：
  · 大海 08:36 跑 → 数据止于 T-1（他的 data_basis 明写"最近已完成交易日"）
  · WB  16:00 跑 → 数据是 T 日收盘
直接对账 = 拿 T 与 T-1 比，出来的全是"数据分歧"，双模型分歧样本被污染。

【做法】
用 **T-1 的事实包**重算 WB 的观点（不写文件、不推送），
与大海当日文件（其数据基准也是 T-1）放在同一基准上比较。
这样剩下的分歧才是"真分歧"。

【输出】
  reconcile/same_basis/{date}_same_basis.json  —— 机器可读
  控制台表格

【用法】
  python3 same_basis_reconcile.py --date 2026-10-20
  python3 same_basis_reconcile.py --date 2026-10-20 --wb-date 2026-10-20   # 若 WB 也是 T 日
"""
import argparse
import json
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

OUT_DIR = os.path.join(BASE_DIR, "reconcile", "same_basis")
FACTS_DIR = os.path.join(BASE_DIR, "facts")
PRED_DIR = os.path.join(BASE_DIR, "predictions")


def _load(name):
    p = os.path.join(FACTS_DIR, name)
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    return None


def prev_trade_day_facts(date):
    """找 <= date 的前一个交易日事实包（用于同基准重算）。"""
    names = sorted(f[:-5] for f in os.listdir(FACTS_DIR)
                   if f.endswith(".json") and f[0].isdigit())
    cands = [n for n in names if n < date]
    return cands[-1] if cands else None


def sem(action):
    if action in ("buy", "buy_probe", "buy_standard"):
        return "买入"
    if action in ("sell",):
        return "卖出"
    if action in ("watch",):
        return "观察"
    return "无操作"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="对账日期（大海文件的日期）")
    ap.add_argument("--wb-date", default=None,
                    help="WB 预测所用日期；默认自动取 date 的前一个交易日")
    args = ap.parse_args()

    import daily_predict as DP

    wb_date = args.wb_date or prev_trade_day_facts(args.date)
    if not wb_date:
        print(f"[ERR] 找不到 {args.date} 之前的事实包")
        return 1

    raw = _load(f"{wb_date}.json")
    pkg = DP._normalize_facts_pkg(raw)
    if pkg is None:
        print(f"[ERR] facts/{wb_date}.json 格式无法识别")
        return 2
    fm = {s["name"]: s for s in pkg.get("stocks", [])}

    # 用 T-1 事实层重算 WB 观点（不落盘）
    wb_views = {}
    for code, _tencent, name in DP.WATCHLIST:
        f = fm.get(name) or fm.get(DP.FACTS_ALIAS.get(name))
        if not f:
            continue
        r = DP._decide(name, code, f.get("close"), f.get("chg_pct") or 0,
                       f.get("ma20_dev"), f.get("rsi6"),
                       (f.get("macd") or {}).get("state"), f.get("vol_ratio"),
                       f.get("dd30"), 1.6, 41.4, source="facts",
                       ten_day_chg=f.get("ten_day_chg"))
        wb_views[name] = {"action": r["action"], "rsi6": f.get("rsi6"),
                          "ten_day_chg": f.get("ten_day_chg"),
                          "core_pool": r["core_pool"]}

    dh_path = os.path.join(PRED_DIR, f"{args.date}_dahai.json")
    if not os.path.exists(dh_path):
        print(f"[ERR] 大海文件不存在：{dh_path}")
        return 3
    dh = json.load(open(dh_path, encoding="utf-8"))

    ALIAS_REV = {v: k for k, v in DP.FACTS_ALIAS.items() if k != v}

    rows, agree, total = [], 0, 0
    for s in dh.get("stock_calls", []):
        key = ALIAS_REV.get(s["name"], s["name"])
        w = wb_views.get(key)
        if not w:
            continue
        same = sem(w["action"]) == sem(s["action"])
        is_core = bool(s.get("core_pool"))
        if is_core:
            total += 1
            agree += int(same)
        rows.append({
            "name": s["name"], "core_pool": is_core,
            "wb_action": w["action"], "dahai_action": s["action"],
            "same_semantic": same,
            "wb_rsi6": w["rsi6"], "dahai_rsi6": s.get("rsi6"),
            "rsi_match": (w["rsi6"] is not None and s.get("rsi6") is not None
                          and abs(w["rsi6"] - s.get("rsi6")) <= 0.01),
        })

    out = {
        "date": args.date,
        "wb_basis_date": wb_date,
        "dahai_basis": dh.get("data_basis"),
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "note": "同基准：WB 用 wb_basis_date 的事实包重算，与大海当日文件在同一数据基准上比较",
        "core_pool": {"agree": agree, "total": total},
        "rsi_exact_match": f"{sum(1 for r in rows if r['rsi_match'])}/{len(rows)}",
        "rows": rows,
        "true_divergences": [r["name"] for r in rows
                             if not r["same_semantic"]],
        "true_divergences_core": [r["name"] for r in rows
                                  if not r["same_semantic"] and r["core_pool"]],
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    dst = os.path.join(OUT_DIR, f"{args.date}_same_basis.json")
    json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"\n{'=' * 72}")
    print(f"同基准对账 {args.date}   WB基准={wb_date}  大海基准={dh.get('data_basis')}")
    print(f"{'=' * 72}")
    print(f"RSI6 逐只一致: {out['rsi_exact_match']}   核心池语义一致: {agree}/{total}")
    print(f"{'-' * 72}")
    for r in rows:
        if not r["core_pool"] and r["same_semantic"]:
            continue
        mk = "✅" if r["same_semantic"] else ("🔴" if r["core_pool"] else "🟡")
        tag = "" if r["core_pool"] else "  (非核心)"
        print(f"  {mk} {r['name']:12s} WB={r['wb_action']:10s} "
              f"大海={r['dahai_action']:10s} RSI {r['wb_rsi6']}/{r['dahai_rsi6']}{tag}")
    print(f"{'=' * 72}")
    print(f"真分歧（核心池）: {out['true_divergences_core'] or '无'}")
    print(f"全部真分歧: {out['true_divergences'] or '无'}")
    print(f"已写入 {os.path.relpath(dst, BASE_DIR)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
