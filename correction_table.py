#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
correction_table.py — 双模型分歧的修正规则（v2.0，2026-10-20）
==============================================================
【v2.0 改了什么】
v1.0 的信号效力是手工常量，基于 7200 根K线的旧回测（FDR 0/24）。
v2.0 改为**从 backtest/signal_stats.json 动态读取**，回测一更新规则自动跟着变。

【当前回测基线（28标的 / 65249根K线 / 约10年）】
  FDR 显著 13/24。关键发现：
    · 超卖类信号 T+5 胜率显著高于 T+1/T+3 → 持有期改 T+5
    · RSI6<15    T+5 58.9% / +1.05% (n=1209)  ← 主力
    · 10日跌超10% T+5 56.0% / +0.87% (n=3078)  ← 辅助
    · RSI6<25    T+5 55.2% / +0.54% (n=5572)  ← 可用但薄
    · MACD金叉   T+5 49.98% → **不是买入信号**（仅作否决）
    · 量比≥1.5   T+5 50.43% → 无效

【修正原则】
  1. 只调仓位，不改方向（双方都无方向 edge）
  2. 信心系数由信号 T+5 胜率映射，再按样本量/FDR/双方一致性打折
  3. 持有周期默认 T+5（实测最优窗口）
  4. MACD 金叉、量比不作买入依据，只作风险否决

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

NAME_ALIAS = {"半导体ETF国联安": "半导体ETF"}


def canon(n):
    return NAME_ALIAS.get(n, n)


def load_stats():
    """读回测结果 → {signal_key: {name, win, ret, n, fdr_t5}}"""
    if not os.path.exists(STATS):
        return {}
    j = json.load(open(STATS, encoding="utf-8"))
    sig = {p["signal"] for p in j.get("fdr", {}).get("passed", [])
           if p["horizon"] == "T+5"}
    out = {}
    for k, v in j.get("signals", {}).items():
        h = v["horizons"].get("T+5")
        if h:
            out[k] = {"name": v["name"], "win": h["win_rate"],
                      "ret": h["avg_ret_pct"], "n": h["n"],
                      "fdr_t5": k in sig}
    return out


def classify(facts_stock, stats):
    """判定命中的信号，多信号取 T+5 胜率最高者。返回 (key, 描述)"""
    rsi = facts_stock.get("rsi6")
    macd = (facts_stock.get("macd") or {}).get("state", "")
    ten = facts_stock.get("ten_day_chg")

    hits = []
    if rsi is not None:
        if rsi < 15:
            hits.append("S2")
        elif rsi < 25:
            hits.append("S1")
    if ten is not None and ten <= -10:
        hits.append("S5")
    if ("S1" in hits or "S2" in hits):
        if macd == "golden_cross":
            hits.append("S8")
        elif macd in ("below", "death_cross"):
            hits.append("S7")

    if not hits:
        return None, f"无有效买入信号（RSI6={rsi}, MACD={macd}, 10日={ten}%）"

    valid = [(k, stats[k]) for k in hits if k in stats and stats[k]["win"] > 50]
    if not valid:
        return None, f"命中信号均无 edge（{hits}）"
    k, s = max(valid, key=lambda x: x[1]["win"])
    return k, (f"{s['name']}：T+5胜率{s['win']}%，均收+{s['ret']}%，"
               f"n={s['n']}{'（过FDR）' if s['fdr_t5'] else ''}")


def confidence_of(sig_key, stats, same_semantic):
    """信心系数 = f(T+5胜率) × 样本量修正 × FDR修正 × 一致性修正"""
    s = stats.get(sig_key)
    if not s:
        return 0.0
    base = max(0.0, min(1.0, (s["win"] - 50.0) / 12.5))   # 50%→0, 62.5%→1.0
    if s["n"] < 200:
        base *= 0.5
    if not s["fdr_t5"]:
        base *= 0.8
    return round(base * (1.0 if same_semantic else 0.5), 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)

    fp = os.path.join(BASE_DIR, "facts", f"{args.date}.json")
    cp = os.path.join(OUT_DIR, "daily_compare.json")
    if not os.path.exists(fp):
        print(f"[ERR] 缺事实包 {fp}，先跑 facts_builder.py --date {args.date}")
        return 1
    facts = json.load(open(fp, encoding="utf-8"))
    # [2026-10-20] 兼容 facts v1（stocks 数组）与 v2（instruments 字典）
    try:
        from daily_predict import _normalize_facts_pkg
        _pkg = _normalize_facts_pkg(facts)
    except Exception:
        _pkg = facts if isinstance(facts.get("stocks"), list) else None
    if not _pkg:
        print(f"[ERR] 事实包格式无法识别 {fp}")
        return 1
    fm = {canon(s["name"]): s for s in _pkg["stocks"]}

    comp = None
    if os.path.exists(cp):
        for r in json.load(open(cp, encoding="utf-8")):
            if r.get("date") == args.date:
                comp = r
    if not comp or comp.get("status") != "ok":
        print("[ERR] 缺当日对账结果，先跑 reconcile_wb_dahai.py")
        return 1

    stats = load_stats()
    if not stats:
        print("[ERR] 缺回测结果，先跑 backtest/signal_stats.py")
        return 1

    rows = []
    for s in comp["stocks"]:
        name = s["name"]
        f = fm.get(canon(name))
        if not f:
            continue
        att = s.get("attribution") or ""
        # [2026-10-20 调整] 数据分歧不再一律阻断——它是"对方输错数"，
        # 不是"没有机会"。处理：信号照判，信心额外打 6 折，并在输出标注。
        is_data_div = att.startswith("数据分歧")
        no_facts = att.startswith("无事实包")
        data_ok = not no_facts
        sig_key, sig_note = classify(f, stats)
        conf = (confidence_of(sig_key, stats, s["same_semantic"])
                if (sig_key and data_ok) else 0.0)
        if conf and is_data_div:
            conf = round(conf * 0.6, 2)

        # [2026-10-20 关键修正] 仓位必须"至少一方看多"。
        # 原逻辑只看 facts 层信号，导致双方都 watch 的标的（金钼 RSI=24.94 命中 S7）
        # 也给出 2.52% 仓位——逻辑矛盾（观察 ≠ 买入）。
        def _bullish(a):
            return a in ("buy", "buy_probe", "buy_standard")
        at_least_one_bullish = _bullish(s.get("wb_action")) or _bullish(s.get("dahai_action"))
        consensus = s.get("same_semantic")

        if not at_least_one_bullish:
            sig_key, conf = None, 0.0
            sig_note = (f"信号命中({sig_note.split('：')[0] if sig_note else '-'})，"
                        f"但双方均未给出买入 → 不建仓")

        kelly = s.get("kelly_pct") or 0
        if not kelly and sig_key:
            kelly = 6.0
        final = round(kelly * conf, 2) if (kelly and conf) else 0.0
        note = ""
        blocked = None
        if 0 < final < 2.0:
            blocked = final
            final = 0.0
            note = f"算出{blocked}%，低于2%门槛→不操作"

        rows.append({
            "name": name, "wb_action": s["wb_action"], "dahai_action": s["dahai_action"],
            "same_semantic": s["same_semantic"], "attribution": s.get("attribution"),
            "rsi6": f.get("rsi6"), "macd": (f.get("macd") or {}).get("state"),
            "ten_day_chg": f.get("ten_day_chg"),
            "signal_key": sig_key, "signal_note": sig_note,
            "data_divergence": is_data_div,
            "data_divergence_note": ("⚠ 数据分歧：对方输入偏离事实层，信心×0.6"
                                     if is_data_div else ""),
            "base_kelly_pct": kelly, "confidence": conf,
            "final_position_pct": final, "blocked_by_gate_pct": blocked,
            "decision_reason": note,
        })

    out = {
        "date": args.date,
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "_basis": "signal_stats.json（28标的/65249根K线/近10年）",
        "_principle": "只调仓位不改方向；MACD金叉与量比不作买入依据（实测<50%）",
        "hold_period": "T+5",
        "positions": rows,
        "total_position_pct": round(sum(r["final_position_pct"] for r in rows), 2),
    }
    dst = os.path.join(OUT_DIR, f"correction-{args.date}.json")
    json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"\n{'='*82}")
    print(f"修正裁决 {args.date}  （持有期 T+5）")
    print(f"{'='*82}")
    print(f"{'标的':10s} {'WB':6s} {'大海':6s} {'RSI6':>6s} {'信号':6s} {'信心':>5s} {'仓位':>6s}")
    print("-" * 82)
    for r in rows:
        print(f"{r['name'][:9]:10s} {r['wb_action']:6s} {r['dahai_action']:6s} "
              f"{r['rsi6'] or 0:6.2f} {(r['signal_key'] or '-'):6s} "
              f"{r['confidence']:5.2f} {r['final_position_pct']:5.2f}%")
        if r["signal_key"]:
            print(f"       └ {r['signal_note']}")
        if r["decision_reason"]:
            print(f"       ⚠ {r['decision_reason']}")
    print(f"\n总仓位: {out['total_position_pct']}%")
    print(f"已写入 {dst}")

    if args.push:
        r = subprocess.run([sys.executable, os.path.join(BASE_DIR, "push_api.py"),
                            f"reconcile/correction-{args.date}.json"],
                           capture_output=True, text=True, timeout=90)
        print("推送:", (r.stdout or "").strip().splitlines()[-1] if r.stdout else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
